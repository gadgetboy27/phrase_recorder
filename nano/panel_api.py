#!/usr/bin/env python3
"""Runs ON THE NANO. The HTTP API the ESP32 touch panel (nano/panel.yaml) talks to.

    python3 ~/panel/panel_api.py          # 0.0.0.0:8765, foreground, logs to stderr

`tools/nano.sh panel` copies this file, asr_worker.py and public/phrases.json to
~/panel/ on the Nano, (re)starts it, and installs an @reboot crontab line so it
survives a power cycle. Stdlib only — nothing to install on the Nano.

Routes (JSON responses; the panel only reads the first ~60 chars of `say`):
  GET  /health                 what the panel depends on: audio card, piper, whisper, llama-server
  GET  /phrases?lang=xx&src=yy {languages, categories, phrases:[{key, category, text, src_text,
                               translation, recording}]} — everything the panel needs to build its
                               screen. `lang` is the patient's language, `src` the clinician's.
  POST /panel/<key>?lang=xx    say phrase <key> in xx. Preference order: a pushed recording by a
                               native/fluent speaker → Piper on the approved translation → a learner's
                               recording → Piper in English. Returns at once; playback runs in the
                               background and a new tap cuts it off.
  POST /panel/record?lang=xx   start recording the USB mic (a second tap restarts the take)
  POST /panel/stop?lang=xx&src=yy&speak=1
                               stop, transcribe with whisper-cli in xx; when yy ≠ xx and llama-server
                               is up, translate the transcript into yy and (speak=1) say it with Piper.
  POST /panel/record?lang=xx&phrase=p003&speaker=hp&type=native&consent=1
                               volunteer take of a phrase: on /panel/stop it is filed as a batch +
                               audio_samples row and whisper's text becomes a golden_set draft
                               (panel_takes.py). Refused without consent=1 or a valid speaker id.
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asr_worker  # noqa: E402  (deployed alongside; whisper-cli + llama helpers)
import panel_takes  # noqa: E402

HERE = Path(__file__).resolve().parent
PHRASES = HERE / "phrases.json"
PIPER = Path.home() / ".local/bin/piper"
VOICES = Path.home() / "piper-voices"
WORK = Path.home() / "phrase-recordings" / "_panel"      # tts cache + panel recordings
AUDIO_DEV = "plughw:0,0"                                # the USB PnP sound device: mic + speaker
WHISPER_MODEL = "ggml-large-v3-turbo-q5_0.bin"
PORT = 8765
# Piper voices on the Nano, by our language code (docs/data-contract.md). No Māori voice yet.
VOICE = {"en": "en_US-lessac-medium", "ar": "ar_JO-kareem-medium", "es": "es_ES-davefx-medium",
         "fa": "fa_IR-amir-medium", "prs": "fa_IR-amir-medium", "hi": "hi_IN-rohan-medium",
         "vi": "vi_VN-vais1000-medium", "zh": "zh_CN-huayan-medium"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def load_phrases():
    d = json.loads(PHRASES.read_text())
    return {p["key"]: p for p in d["phrases"]}, {l["code"]: l["label"] for l in d["languages"]}


RECORDINGS = Path.home() / "phrase-recordings"
SPEAKER_RANK = {"native": 0, "fluent": 1, "learner": 2}


def recording_index():
    """{(phrase_key, lang): [(rank, recorded_at, path), ...]} from every pushed batch's manifest,
    best first. Batches are what push.py delivered; `_*` dirs (tts cache, panel takes) are skipped."""
    idx = {}
    for mf in RECORDINGS.glob("[!_.]*/manifest.json"):
        try:
            m = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        rank = SPEAKER_RANK.get(m.get("speaker", {}).get("type"), 9)
        for r in m.get("recordings", []):
            if r.get("unmatched") or not r.get("phrase_key"):
                continue
            wav = mf.parent / r["file"]
            if wav.exists():
                idx.setdefault((r["phrase_key"], m["language"]), []).append((rank, r.get("recorded_at", ""), wav))
    for v in idx.values():
        v.sort(key=lambda t: t[1], reverse=True)   # newest first …
        v.sort(key=lambda t: t[0])                 # … within native → fluent → learner (stable)
    return idx


class Audio:
    """One speaker, one mic: a new phrase cuts the previous one off; one take at a time."""
    def __init__(self):
        self.lock = threading.Lock()
        self.player = None
        self.recorder = None
        self.take = None
        self.take_meta = None       # None for a free transcription, else the volunteer/phrase context

    def say(self, text, lang):
        voice = VOICE.get(lang, VOICE["en"])
        wav = WORK / "tts" / f"{voice}_{hashlib.sha1(text.encode()).hexdigest()[:12]}.wav"
        if not wav.exists():
            wav.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([str(PIPER), "--model", str(VOICES / f"{voice}.onnx"), "--output_file", str(wav)],
                           input=text, text=True, check=True, capture_output=True)
        self.play(wav)

    def play(self, wav):
        with self.lock:
            if self.player and self.player.poll() is None:
                self.player.terminate()
                self.player.wait()          # the card is only free once aplay has gone
            self.player = subprocess.Popen(["aplay", "-q", "-D", AUDIO_DEV, str(wav)])

    def record(self, meta=None):
        with self.lock:
            self._stop_recorder()
            WORK.mkdir(parents=True, exist_ok=True)
            self.take_meta = dict(meta or {}, started_at_ms=int(time.time() * 1000))
            self.take = WORK / time.strftime("%Y%m%dT%H%M%SZ_take.wav", time.gmtime())
            self.recorder = subprocess.Popen(
                ["arecord", "-q", "-D", AUDIO_DEV, "-f", "S16_LE", "-r", "48000", "-c", "1", str(self.take)])
            return self.take

    def stop(self):
        with self.lock:
            if not self.recorder:
                return None
            self._stop_recorder()
            take, self.take = self.take, None
            meta, self.take_meta = self.take_meta, None
            return take, meta

    def _stop_recorder(self):
        if self.recorder and self.recorder.poll() is None:
            self.recorder.send_signal(signal.SIGINT)   # arecord finalises the WAV header on SIGINT
            self.recorder.wait(timeout=5)
        self.recorder = None

    @property
    def recording(self):
        return bool(self.recorder and self.recorder.poll() is None)


audio = Audio()


def llama_up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1) as r:
            return b"ok" in r.read()
    except Exception:
        return False


def to_language(text, language, target="English"):
    body = {"messages": [
        {"role": "system", "content": f"Translate the patient's {language} sentence into plain {target}. "
                                      "Reply with the translation only."},
        {"role": "user", "content": text}], "temperature": 0, "max_tokens": 120}
    req = urllib.request.Request(asr_worker.LLAMA_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def health():
    return {
        "audio": Path("/proc/asound/card0").exists(),
        "piper": PIPER.exists(),
        "whisper": (asr_worker.WHISPER / "models" / WHISPER_MODEL).exists(),
        "llama": llama_up(),
        "db": panel_takes.db_ok(),
        "recording": audio.recording,
        "phrases": len(load_phrases()[0]),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):          # one line per request, our format (the pair is in the query)
        log(self.address_string(), fmt % args)

    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            return self.reply(200, health())
        if u.path == "/phrases":
            d = json.loads(PHRASES.read_text())
            lang, src = q.get("lang", "en"), q.get("src", "en")
            recs = recording_index()
            return self.reply(200, {
                "languages": d["languages"],
                "categories": d["categories"],
                "phrases": [{"key": p["key"], "category": p["category"], "text": p["text"],
                             "src_text": p["text"] if src == "en" else p["translations"].get(src),
                             "translation": p["translations"].get(lang),
                             "recording": bool(recs.get((p["key"], lang)))} for p in d["phrases"]],
            })
        self.reply(404, {"error": "no such route"})

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.rfile.read(int(self.headers.get("Content-Length") or 0))   # panel body is irrelevant
        if not u.path.startswith("/panel/"):
            return self.reply(404, {"error": "no such route"})
        action = u.path[len("/panel/"):]
        lang = q.get("lang", "en")
        try:
            if action == "record":
                meta = None
                if q.get("phrase"):                                   # a volunteer take of a phrase
                    phrases, _ = load_phrases()
                    p = phrases.get(q["phrase"])
                    sid = q.get("speaker", "").lower()
                    if not p:
                        return self.reply(404, {"say": f"unknown phrase {q['phrase']}"})
                    if q.get("consent") != "1":
                        return self.reply(409, {"say": "Consent not confirmed"})
                    if not (2 <= len(sid) <= 12 and sid.isalnum()):
                        return self.reply(409, {"say": "Speaker id: 2–12 letters/digits"})
                    if q.get("type", "native") not in ("native", "fluent", "learner", "staff"):
                        return self.reply(409, {"say": "Speaker type?"})
                    tr = p["translations"].get(lang)
                    meta = {"phrase": p, "lang": lang, "speaker": {"id": sid, "type": q.get("type", "native")},
                            "read_text": p["text"] if lang == "en" else tr,
                            "translation_source": "canonical" if lang == "en" else ("approved" if tr else None)}
                take = audio.record(meta)
                say = "Recording… tap Stop" if not meta else "Read it aloud, then tap Stop"
                return self.reply(200, {"say": say, "file": take.name, "read_text": meta and meta["read_text"]})
            if action == "stop":
                take, meta = audio.stop()
                if not take:
                    return self.reply(409, {"say": "Not recording"})
                if meta and meta.get("phrase"):                       # file the take, then draft its text
                    batch_id, rec = panel_takes.file_take(take, meta["started_at_ms"], meta["lang"], meta["speaker"],
                                                          meta["phrase"], meta["read_text"], meta["translation_source"])
                    wav = panel_takes.RECORDINGS / batch_id / rec["file"]
                    wl = asr_worker.WHISPER_LANG.get(lang, lang)
                    text = asr_worker.transcribe(WHISPER_MODEL, wl, [wav])[str(wav)]
                    panel_takes.draft_transcript(batch_id, rec, lang, meta["phrase"], text, WHISPER_MODEL)
                    say = f"Take {rec['take']} saved ({rec['duration_ms'] / 1000:.1f} s)"
                    if text and lang != "en":
                        say += f" — draft: {text}"
                    log(f"take filed: {batch_id}/{rec['file']}")
                    return self.reply(200, {"say": say, "text": text, "file": rec["file"], "batch": batch_id, "take": rec["take"]})
                wl = asr_worker.WHISPER_LANG.get(lang, lang)
                text = asr_worker.transcribe(WHISPER_MODEL, wl, [take])[str(take)]
                out = {"say": text or "(nothing heard)", "text": text, "file": take.name}
                src = q.get("src", "en")
                if text and lang != src and llama_up():
                    names = load_phrases()[1]
                    out["translated"] = to_language(text, names.get(lang, lang), names.get(src, "English"))
                    out["say"] = f"{text} → {out['translated']}"
                    if q.get("speak") == "1" and src in VOICE:
                        audio.say(out["translated"], src)
                elif text and lang != src:
                    out["say"] = f"{text}  (llama-server is off — no translation)"
                return self.reply(200, out)
            phrases, _ = load_phrases()
            p = phrases.get(action)
            if not p:
                return self.reply(404, {"say": f"unknown phrase {action}"})
            tr = p["text"] if lang == "en" else p["translations"].get(lang)   # English is the source, not a translation
            recs = recording_index().get((p["key"], lang), []) if lang != "en" else []
            best = recs[0] if recs else None
            if best and best[0] <= SPEAKER_RANK["fluent"]:            # a real speaker of the language
                audio.play(best[2])
                return self.reply(200, {"say": tr or p["text"], "how": "recording", "key": action, "lang": lang,
                                        "file": best[2].name, "note": None})
            if tr and lang in VOICE:                                  # synthesised in the language
                audio.say(tr, lang)
                return self.reply(200, {"say": tr, "how": "piper", "key": action, "lang": lang, "note": None})
            if best:                                                  # only a learner's take, still the language
                audio.play(best[2])
                return self.reply(200, {"say": f"{tr or p['text']}  (learner recording)", "how": "recording",
                                        "key": action, "lang": lang, "file": best[2].name, "note": "learner recording"})
            audio.say(p["text"], "en")                                # fall back to English
            if tr:
                say, note = f"{tr}  (said in English — no {lang} voice)", "no Piper voice for " + lang
            elif lang == "en":
                say, note = p["text"], None
            else:
                say, note = f"{p['text']}  (no {lang} translation yet)", "no approved translation"
            return self.reply(200, {"say": say, "how": "piper", "key": action, "lang": "en", "note": note})
        except subprocess.CalledProcessError as e:
            log("subprocess failed:", e.cmd[0], (e.stderr or b"")[-300:])
            return self.reply(500, {"say": f"{e.cmd[0]} failed"})
        except Exception as e:                      # keep serving; the panel shows the message
            log("error:", repr(e))
            return self.reply(500, {"say": f"error: {e}"})


if __name__ == "__main__":
    log(f"panel api on :{PORT}", json.dumps(health()))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
