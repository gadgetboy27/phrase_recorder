#!/usr/bin/env python3
"""Runs ON THE NANO. The HTTP API the ESP32 touch panel (nano/panel.yaml) talks to.

    python3 ~/panel/panel_api.py          # 0.0.0.0:8765, foreground, logs to stderr

`tools/nano.sh panel` copies this file, asr_worker.py and public/phrases.json to
~/panel/ on the Nano, (re)starts it, and installs an @reboot crontab line so it
survives a power cycle. Stdlib only — nothing to install on the Nano.

Routes (JSON responses; the panel only reads the first ~60 chars of `say`):
  GET  /render?text=&lang=&size=&w=&fg=&bg=&align=   PNG of shaped text for scripts the panel can't draw
  GET  /render/replies?lang=&t0=&s0=&a0=1&t1=…       PNG strip of reply cells (sits behind the panel's buttons)
  GET  /health                 what the panel depends on: audio card, piper, whisper, llama-server
  GET  /phrases?lang=xx&src=yy {languages, categories, phrases:[{key, category, text, src_text,
                               translation, recording}]} — everything the panel needs to build its
                               screen. `lang` is the patient's language, `src` the clinician's.
  POST /panel/<key>?lang=xx    say phrase <key> in xx. Preference order: a pushed recording by a
                               native/fluent speaker → Piper on the approved translation → a learner's
                               recording → Piper in English. Returns at once; playback runs in the
                               background and a new tap cuts it off. `&repeat=1` marks the patient
                               page's "Hear it again" in the session log.
  POST /panel/session?src=yy&dst=xx   start a session (turn log under ~/panel/sessions/)
  POST /panel/reply/<key>?lang=xx&src=yy
                               a preset patient reply: say phrase <key> in the clinician's language yy
  POST /panel/shutdown         power the Nano off (the panel's start screen has a hold-to-shut-down
                               button so a demo kit can be closed without a laptop)
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
import re
import mimetypes
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                                    # pip3 install --user gTTS — Google's voices for languages Piper lacks
    from gtts import gTTS
except ImportError:
    gTTS = None
try:                                    # pip3 install --user arabic-reshaper python-bidi
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:                     # panel shows unshaped letters until installed
    arabic_reshaper = get_display = None
import asr_worker  # noqa: E402  (deployed alongside; whisper-cli + llama helpers)
import panel_takes  # noqa: E402
import panel_drafts  # noqa: E402
import panel_render  # noqa: E402
import panel_admin  # noqa: E402

# The language table lives in panel_admin; the other modules learn the extra codes/fonts from it.
panel_drafts.NLLB_CODES.update(panel_admin.NLLB_CODES)
panel_render.FONT_FOR.update({c: (panel_admin.font_path(c), 0) for c in panel_admin.LANGUAGES if panel_admin.font_path(c)})
panel_render.RTL.update(panel_admin.RTL)
asr_worker.WHISPER_UNSUPPORTED.setdefault("fj", "Fijian")

HERE = Path(__file__).resolve().parent
PHRASES = HERE / "phrases.json"
PIPER = Path.home() / ".local/bin/piper"
VOICES = Path.home() / "piper-voices"
WORK = Path.home() / "phrase-recordings" / "_panel"      # tts cache + panel recordings
WEB = HERE / "web"                                        # the iPad client (nano/web/), served at /
TLS_PORT = 8766                                           # HTTPS twin of :8765 — iPads need it for the mic
RUNTIME = Path("/run/phrase-panel")                       # exists when the systemd unit runs us (nano/install.sh)
MAX_UPLOAD = 60 * 1024 * 1024                             # a WAV from the iPad: ~10 minutes at 48 kHz mono
# Paths any client may fetch without a device token: the setup page and what it needs, audio/render
# fetches (their ids are unguessable / their content is what the caller sent), health, pairing.
PUBLIC_GET = {"/", "/index.html", "/app.js", "/app.css", "/setup", "/setup.html", "/manifest.webmanifest", "/sw.js",
              "/icon.svg", "/icon-192.png", "/icon-512.png", "/ca.crt", "/health", "/render", "/render/replies"}
PUBLIC_POST = {"/pair"}
AUDIO_IDS = {}                                            # unguessable id -> Path, for GET /audio/<id>
AUDIO_DEV = "plughw:0,0"                                # the USB PnP sound device: mic + speaker
WHISPER_MODEL = "ggml-large-v3-turbo-q5_0.bin"
PORT = 8765
BEACON_PORT = 18511      # UDP: every 3 s, on every interface, {"nano": "http://<ip>:8765"} so the panel finds us on any Wi-Fi
# Demo mode: speak an unverified machine translation (NLLB draft) when no approved text exists.
# The panel still shows it amber as unverified. Set to False for clinical use.
SPEAK_DRAFTS = True
# Google TTS for the languages Piper has no voice for. Text is sent to Google once and the WAV
# cached (WORK/tts_google), so pre-generate at home (GET /pregen?lang=xx) and the demo stays offline.
# Synthetic audio never goes into audio_samples — it is playback only, not training data.
GTTS_LANG = {"yue": "yue", "ja": "ja", "ko": "ko", "pa": "pa", "tl": "tl"}
# Piper voices on the Nano, by our language code (docs/data-contract.md). No Māori voice yet.
VOICE = {"en": "en_US-lessac-medium", "ar": "ar_JO-kareem-medium", "es": "es_ES-davefx-medium",
         "fa": "fa_IR-amir-medium", "prs": "fa_IR-amir-medium", "hi": "hi_IN-rohan-medium",
         "vi": "vi_VN-vais1000-medium", "zh": "zh_CN-huayan-medium"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


ARABIC_SCRIPT = {"ar", "fa", "prs"}


def for_panel(text, lang):
    """Text as the panel's LVGL can draw it: Arabic script pre-joined and put in display order
    (the panel has no bidi engine — enabling LVGL's crashed it). Everything else passes through."""
    if text and lang in ARABIC_SCRIPT and arabic_reshaper:
        return get_display(arabic_reshaper.reshape(text))
    return text


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


def serve_audio(path):
    """URL the client can fetch this WAV from. Ids are keyed on the path, so the same file gets the same URL."""
    aid = hashlib.sha256(str(path).encode() + b"phrasekit").hexdigest()[:32]
    AUDIO_IDS[aid] = Path(path)
    return f"/audio/{aid}"


class Audio:
    """One speaker, one mic: a new phrase cuts the previous one off; one take at a time.
    Every play/say takes `client`: True means "synthesise but do not play — return the WAV paths",
    for an iPad that plays through its own speaker."""
    def __init__(self):
        self.lock = threading.Lock()
        self.player = None
        self.recorder = None
        self.take = None
        self.take_meta = None       # None for a free transcription, else the volunteer/phrase context

    def synth(self, text, lang):
        """WAV for text in lang: Piper when it has the voice, else Google (cached), else Piper English."""
        h = hashlib.sha1(text.encode()).hexdigest()[:12]
        if lang not in VOICE and lang in GTTS_LANG and gTTS:
            wav = WORK / "tts_google" / f"{lang}_{h}.wav"
            if not wav.exists():
                wav.parent.mkdir(parents=True, exist_ok=True)
                mp3 = wav.with_suffix(".mp3")
                gTTS(text=text, lang=GTTS_LANG[lang]).save(str(mp3))          # needs internet; cached after
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp3), "-ar", "22050", "-ac", "1", str(wav)], check=True)
                mp3.unlink()
            return wav
        voice = VOICE.get(lang, VOICE["en"])
        wav = WORK / "tts" / f"{voice}_{h}.wav"
        if not wav.exists():
            wav.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([str(PIPER), "--model", str(VOICES / f"{voice}.onnx"), "--output_file", str(wav)],
                           input=text, text=True, check=True, capture_output=True)
        return wav

    def say(self, text, lang, then=None, client=False):
        """Speak text; `then` = (text, lang) to say straight after (e.g. a warning, then the phrase).
        Synthesis + playback run in the background so the panel gets its reply immediately.
        client=True: synthesise now and return the WAV paths for the caller to play."""
        if client:
            return [self.synth(text, lang)] + ([self.synth(*then)] if then else [])
        def run():
            try:
                wavs = [self.synth(text, lang)] + ([self.synth(*then)] if then else [])
                self.play(*wavs)
            except subprocess.CalledProcessError as e:
                log("piper failed:", (e.stderr or b"")[-200:])
        threading.Thread(target=run, daemon=True).start()

    def play(self, *wavs, client=False):
        if client:
            return list(wavs)
        with self.lock:
            if self.player and self.player.poll() is None:
                self.player.terminate()
                self.player.wait()          # the card is only free once aplay has gone
            self.player = subprocess.Popen(["aplay", "-q", "-D", AUDIO_DEV, *map(str, wavs)])

    def record(self, meta=None):
        with self.lock:
            self._stop_recorder()
            WORK.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - 3600                    # free takes are transient: drop yesterday's, keep the
            for old in WORK.glob("*_take.wav"):            # one "Add phrase" may still be about to file
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
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


def to_language(text, language, target="English", src_code=None, tgt_code=None):
    """What a patient said, in the clinician's language: NLLB when we have it, else the llama-server."""
    if src_code and tgt_code:
        out = panel_drafts.nllb_translate(text, src_code, tgt_code)
        if out:
            return out
    body = {"messages": [
        {"role": "system", "content": f"Translate the patient's {language} sentence into plain {target}. "
                                      "Reply with the translation only."},
        {"role": "user", "content": text}], "temperature": 0, "max_tokens": 120}
    req = urllib.request.Request(asr_worker.LLAMA_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def has_voice(lang):
    return lang in VOICE or (lang in GTTS_LANG and gTTS is not None)


def pregen(lang):
    """Synthesise every phrase (approved text or draft) in lang into the cache — run at home before a demo."""
    phrases, _ = load_phrases()
    n = 0
    for p in phrases.values():
        text = p["translations"].get(lang) or panel_drafts.get(p, lang)
        if text:
            try:
                audio.synth(text, lang); n += 1
            except Exception as e:
                log("pregen failed:", p["key"], lang, repr(e))
    log(f"pregen {lang}: {n} phrases cached")


def speak_phrase(p, lang, names, client=False):
    """Say phrase p in lang: native/fluent recording → Piper on the approved text → learner recording →
    English with a spoken warning. Returns the panel's reply dict; with client=True it carries
    "audio": [urls] for the client to play in order instead of the Nano playing them."""
    out = _speak_phrase(p, lang, names, client)
    wavs = out.pop("_wavs", None) or []
    if client:
        out["audio"] = [serve_audio(w) for w in wavs]
    return out


def _speak_phrase(p, lang, names, client):
    tr = p["text"] if lang == "en" else p["translations"].get(lang)
    recs = recording_index().get((p["key"], lang), []) if lang != "en" else []
    best = recs[0] if recs else None
    lname = names.get(lang, lang)
    shown = for_panel(tr, lang) or p["text"]
    if best and best[0] <= SPEAKER_RANK["fluent"]:
        w = audio.play(best[2], client=client)
        return {"say": shown, "how": "recording", "lang": lang, "file": best[2].name, "note": None, "_wavs": w}
    if tr and has_voice(lang):
        w = audio.say(tr, lang, client=client)
        return {"say": shown, "how": "piper" if lang in VOICE else "google", "lang": lang, "note": None, "_wavs": w}
    if best:
        w = audio.play(best[2], client=client)
        return {"say": shown, "how": "recording", "lang": lang, "file": best[2].name, "note": "learner recording", "_wavs": w}
    draft = panel_drafts.get(p, lang)
    if SPEAK_DRAFTS and draft and has_voice(lang) and lang != "en":     # demo: voice the unverified draft
        w = audio.say(draft, lang, client=client)
        return {"say": for_panel(draft, lang), "how": ("piper" if lang in VOICE else "google") + "-draft", "lang": lang,
                "note": "unverified — machine translation", "draft": for_panel(draft, lang), "offer_record": True, "_wavs": w}
    # English fallback — and say so, so nobody mistakes it for the translation
    if tr:
        warn, note = f"Sorry, I can't say that in {lname} yet.", f"No {lname} voice"
    else:
        warn, note = f"Sorry, I don't have that phrase in {lname} yet.", f"No {lname} version yet"
    w = audio.say(warn, "en", then=(p["text"], "en"), client=client)
    return {"say": shown, "how": "piper", "lang": "en", "note": note, "warning": warn,
            "offer_record": True, "draft": for_panel(panel_drafts.get(p, lang), lang), "_wavs": w}


def take_meta(q, lang):
    """The volunteer/phrase context of a take from the query, or None for a free transcription.
    Raises ValueError(status, message) when the request is not a valid take."""
    if not q.get("phrase"):
        return None
    phrases, _ = load_phrases()
    p = phrases.get(q["phrase"])
    sid = q.get("speaker", "").lower()
    if not p:
        raise ValueError(404, f"unknown phrase {q['phrase']}")
    if q.get("consent") != "1":
        raise ValueError(409, "Consent not confirmed")
    if not (2 <= len(sid) <= 12 and sid.isalnum()):
        raise ValueError(409, "Speaker id: 2–12 letters/digits")
    if q.get("type", "native") not in ("native", "fluent", "learner", "staff"):
        raise ValueError(409, "Speaker type?")
    tr = p["translations"].get(lang)
    return {"phrase": p, "lang": lang, "speaker": {"id": sid, "type": q.get("type", "native")},
            "read_text": p["text"] if lang == "en" else tr,
            "translation_source": "canonical" if lang == "en" else ("approved" if tr else None)}


def finish_take(take, meta, q, lang, client=False, device=panel_takes.PANEL_DEVICE):
    """A finished WAV: file it as a take (meta) or transcribe/translate it (free speech).
    Returns (status, reply). client=True returns the spoken reply as audio URLs instead of playing it;
    device is what recorded the WAV (the Nano's mic, or the uploading iPad's user agent)."""
    if lang in asr_worker.WHISPER_UNSUPPORTED and not (meta and meta.get("phrase")):
        return 200, {"say": f"Whisper has no {asr_worker.WHISPER_UNSUPPORTED[lang]} model yet — "
                            "recordings of phrases in it are what will make that possible.", "text": None}
    raw = q.get("raw") == "1"
    shape = (lambda t, l: t) if raw else for_panel
    if meta and meta.get("phrase"):                       # file the take, then draft its text
        batch_id, rec = panel_takes.file_take(take, meta["started_at_ms"], meta["lang"], meta["speaker"],
                                              meta["phrase"], meta["read_text"], meta["translation_source"], device)
        wav = panel_takes.RECORDINGS / batch_id / rec["file"]
        wl = asr_worker.WHISPER_LANG.get(lang, lang)
        text = asr_worker.transcribe(WHISPER_MODEL, wl, [wav])[str(wav)]
        panel_takes.draft_transcript(batch_id, rec, lang, meta["phrase"], text, WHISPER_MODEL)
        say = f"Take {rec['take']} saved ({rec['duration_ms'] / 1000:.1f} s)"
        if text and lang != "en":
            say += f" — draft: {text}"
        log(f"take filed: {batch_id}/{rec['file']}")
        return 200, {"say": say, "text": text, "file": rec["file"], "batch": batch_id, "take": rec["take"]}
    wl = asr_worker.WHISPER_LANG.get(lang, lang)
    text = asr_worker.transcribe(WHISPER_MODEL, wl, [take])[str(take)]
    out = {"say": shape(text, lang) or "(nothing heard)", "text": text, "file": take.name}
    src = q.get("src", "en")
    if text and lang != src and (llama_up() or panel_drafts.nllb_available()):
        names = load_phrases()[1]
        out["translated"] = to_language(text, names.get(lang, lang), names.get(src, "English"), lang, src)
        out["say"] = f"{shape(text, lang)} → {shape(out['translated'], src)}"
        if q.get("speak") == "1" and has_voice(src):
            w = audio.say(out["translated"], src, client=client)
            if client:
                out["audio"] = [serve_audio(x) for x in w]
        panel_drafts.log_turn("patient" if q.get("who") == "patient" else "clinician", kind="speech",
                              sourceText=text, translatedText=out.get("translated"))
    elif text and lang != src:
        out["say"] = f"{text}  (no translator running — NLLB missing and llama-server off)"
    return 200, out


def beacon():
    """Broadcast our URL on each interface's subnet so the panel can find the Nano on whatever
    network they both joined (home LAN, a phone hotspot, the PhraseKit hotspot). No fixed IPs."""
    while True:
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                parts = line.split()
                if "brd" not in parts or parts[1] in ("lo", "docker0"):
                    continue
                ip, bcast = parts[3].split("/")[0], parts[parts.index("brd") + 1]
                msg = json.dumps({"nano": f"http://{ip}:{PORT}"}).encode()
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(msg, (bcast, BEACON_PORT))
                s.close()
        except Exception as e:
            log("beacon:", repr(e))
        time.sleep(3)


def health():
    return {
        "audio": Path("/proc/asound/card0").exists(),
        "piper": PIPER.exists(),
        "whisper": (asr_worker.WHISPER / "models" / WHISPER_MODEL).exists(),
        "llama": llama_up(),
        "db": panel_takes.db_ok(),
        "drafts_pending": panel_drafts.pending(),
        "translator": "nllb" if panel_drafts.nllb_available() else ("qwen" if llama_up() else None),
        "recording": audio.recording,
        "phrases": len(load_phrases()[0]),
        "wifi": panel_admin.wifi(),                    # which network the Nano is on, and its address there
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

    def send_png(self, path):
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- who is asking: a paired device, or one of the public paths ----
    def token(self, q):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.startswith("Bearer ") else q.get("token")

    def authed(self, u, q, public):
        if u.path in public or u.path.startswith("/audio/"):
            return True
        if panel_admin.token_ok(self.token(q)):
            return True
        self.reply(401, {"error": "not paired", "say": "This device is not paired with the Nano — open /setup"})
        return False

    def send_file(self, path, ctype=None, cache="no-cache"):
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self.authed(u, q, PUBLIC_GET):
            return
        if u.path in ("/", "/index.html", "/setup"):    # the iPad client and its setup/pairing page
            f = WEB / ("setup.html" if u.path == "/setup" else "index.html")
            return self.send_file(f, "text/html; charset=utf-8") if f.exists() else self.reply(404, {"error": "web client not deployed"})
        if u.path in PUBLIC_GET and u.path.count("/") == 1 and "." in u.path and (WEB / u.path[1:]).exists():
            ctype = {"/sw.js": "application/javascript", "/manifest.webmanifest": "application/manifest+json"}.get(u.path)
            return self.send_file(WEB / u.path[1:], ctype, cache="no-cache" if u.path.endswith((".js", ".webmanifest")) else "max-age=86400")
        if u.path == "/ca.crt":                          # the Nano's CA, for the iPad to trust once
            ca = panel_admin.TLS / "ca.crt"
            if not ca.exists():
                return self.reply(404, {"error": "no CA yet — run tools/nano.sh panel"})
            return self.send_file(ca, "application/x-x509-ca-cert", cache="max-age=3600")
        if u.path.startswith("/audio/"):                 # WAVs the API told a client to play
            wav = AUDIO_IDS.get(u.path[len("/audio/"):])
            if not wav or not wav.exists():
                return self.reply(404, {"error": "no such audio"})
            return self.send_file(wav, "audio/wav", cache="max-age=600")
        if u.path == "/pregen":                         # cache Google/Piper audio for a language (background)
            lang = q.get("lang", "")
            threading.Thread(target=pregen, args=(lang,), daemon=True).start()
            return self.reply(200, {"say": f"caching {lang} audio in the background — see ~/panel/panel.log"})
        if u.path == "/render":
            png = panel_render.render_text(q.get("text", ""), q.get("lang", "en"), int(q.get("size", 28)), int(q.get("w", 760)),
                                           q.get("fg", "1B2621"), q.get("bg", "F6F4EF"), q.get("align", "center"), int(q.get("lines", 3)))
            return self.send_png(png)
        if u.path == "/render/replies":
            cells = []
            for i in range(8):
                if f"t{i}" in q:
                    cells.append({"t": q[f"t{i}"], "s": q.get(f"s{i}", ""), "amber": q.get(f"a{i}") == "1"})
            png = panel_render.render_grid(cells, q.get("lang", "en"), int(q.get("w", 776)), int(q.get("h", 168)),
                                           fg=q.get("fg", "1B2621"), bg=q.get("bg", "F6F4EF"))
            return self.send_png(png)
        if u.path == "/admin/languages":                # candidates for the panel's "Add a language"
            return self.reply(200, {"languages": panel_admin.available()})
        if u.path == "/health":
            return self.reply(200, health())
        if u.path == "/admin/wifi":                     # the Wi-Fi picker: networks the Nano knows
            return self.reply(200, {"networks": panel_admin.wifi_networks(), "current": panel_admin.wifi()})
        if u.path == "/devices":                        # paired devices (names only)
            return self.reply(200, {"devices": [dict(v, token=t[:6] + "…") for t, v in panel_admin.devices().items()]})
        if u.path == "/phrases":
            d = json.loads(PHRASES.read_text())
            lang, src = q.get("lang", "en"), q.get("src", "en")
            names = {l["code"]: l["label"] for l in d["languages"]}
            up = llama_up()
            panel_drafts.request(d["phrases"], lang, names.get(lang, lang), up)     # unverified, for display only
            panel_drafts.request(d["phrases"], src, names.get(src, src), up)
            recs = recording_index()
            shape = (lambda t, l: t) if q.get("raw") == "1" else for_panel
            def slim(p):
                row = {"key": p["key"], "category": p["category"], "text": p["text"]}
                if src != "en":
                    if p["translations"].get(src):
                        row["src_text"] = shape(p["translations"][src], src)
                    elif panel_drafts.get(p, src):
                        row["src_draft"] = shape(panel_drafts.get(p, src), src)
                if lang != "en":
                    if p["translations"].get(lang):
                        row["translation"] = shape(p["translations"][lang], lang)
                    elif panel_drafts.get(p, lang):
                        row["draft"] = shape(panel_drafts.get(p, lang), lang)
                if recs.get((p["key"], lang)):
                    row["recording"] = True
                return row
            return self.reply(200, {
                "languages": [dict(l, script=panel_admin.script(l["code"])) for l in d["languages"]],
                "categories": d["categories"],
                "drafts_pending": panel_drafts.pending(),
        "translator": "nllb" if panel_drafts.nllb_available() else ("qwen" if llama_up() else None),
                "phrases": [slim(p) for p in d["phrases"]],
            })
        self.reply(404, {"error": "no such route"})

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.reply(413, {"say": "That recording is too long"})
        body = self.rfile.read(length)                   # a WAV for /panel/transcribe; otherwise ignored
        if not self.authed(u, q, PUBLIC_POST):
            return
        if u.path == "/pair":                            # setup page: pairing code → device token
            token = panel_admin.redeem_pair_code(q.get("code", ""), q.get("name", ""))
            if not token:
                return self.reply(403, {"say": "Wrong or expired code — run tools/nano.sh pair for a new one"})
            log(f"device paired: {q.get('name', 'iPad')!r}")
            return self.reply(200, {"token": token, "say": "Paired"})
        if not u.path.startswith("/panel/"):
            return self.reply(404, {"error": "no such route"})
        action = u.path[len("/panel/"):]
        lang = q.get("lang", "en")
        client = q.get("client") == "1"                  # the caller plays audio itself (iPad)
        try:
            if action == "session":
                sid = panel_drafts.start_session(q.get("src", "en"), q.get("dst", "?"))
                return self.reply(200, {"say": "Session started", "session": sid})
            if action.startswith("reply/"):                           # preset patient reply → clinician's language
                phrases, names = load_phrases()
                p = phrases.get(action[len("reply/"):])
                if not p:
                    return self.reply(404, {"say": "unknown reply"})
                src = q.get("src", "en")
                out = speak_phrase(p, src, names, client)
                out["key"] = p["key"]
                out["patient_text"] = for_panel(p["translations"].get(lang), lang) or p["text"]
                panel_drafts.log_turn("patient", kind="preset", key=p["key"], sourceText=out["patient_text"],
                                      translatedText=out["say"], how=out["how"])
                return self.reply(200, out)
            if action == "phrase":                                    # "Add phrase" page: a new English phrase
                speaker = {"id": q["speaker"].lower(), "type": q.get("type", "staff")} if q.get("speaker") else None
                take = WORK / q["file"] if re.fullmatch(r"\d{8}T\d{6}Z(_\d+)?_take\.wav", q.get("file", "")) else None
                device = panel_takes.PANEL_DEVICE if q.get("device") == "panel" else (self.headers.get("User-Agent") or "unknown client")[:200] + " via PhraseKit web"
                code, out = panel_admin.add_phrase(q.get("text"), q.get("category", ""), PHRASES, speaker, take, device)
                if code == 200:
                    log(f"phrase added from the panel: {out['key']} [{q.get('category')}] {q.get('text')!r}")
                    panel_drafts.log_turn("system", kind="phrase_added", key=out["key"], sourceText=q.get("text"))
                return self.reply(code, out)
            if action == "language":                                  # "Add phrase" page: a new language
                code, out = panel_admin.add_language(q.get("code", ""), PHRASES)
                if code == 200:
                    log(f"language added from the panel: {out['code']}")
                return self.reply(code, out)
            if action == "wifi":                                      # move the Nano to another known network
                code, out = panel_admin.wifi_request(q.get("ssid", ""))
                if code == 200:
                    log(f"wifi switch requested: {out['ssid']}")
                return self.reply(code, out)
            if action == "shutdown":
                log("shutdown requested by the panel")
                self.reply(200, {"say": "Shutting down"})
                if RUNTIME.is_dir():                                          # sandboxed: a root path unit watches for this
                    (RUNTIME / "shutdown").touch()
                else:
                    subprocess.Popen(["sudo", "-n", "shutdown", "-h", "+0"])  # passwordless via /etc/sudoers.d/gadgetboy-power
                return
            if action == "record":                                    # the Nano's own mic
                try:
                    meta = take_meta(q, lang)
                except ValueError as e:
                    return self.reply(e.args[0], {"say": e.args[1]})
                take = audio.record(meta)
                say = "Recording… tap Stop" if not meta else "Read it aloud, then tap Stop"
                return self.reply(200, {"say": say, "file": take.name, "read_text": meta and meta["read_text"]})
            if action == "stop":
                take, meta = audio.stop()
                if not take:
                    return self.reply(409, {"say": "Not recording"})
                return self.reply(*finish_take(take, meta, q, lang, client))
            if action == "transcribe":                                # a WAV recorded by the client (iPad mic)
                if len(body) < 1000 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
                    return self.reply(400, {"say": "Send a WAV file as the request body"})
                try:
                    meta = take_meta(q, lang)
                except ValueError as e:
                    return self.reply(e.args[0], {"say": e.args[1]})
                WORK.mkdir(parents=True, exist_ok=True)
                take = WORK / time.strftime("%Y%m%dT%H%M%SZ_take.wav", time.gmtime())
                n = 0
                while take.exists():                                  # two uploads in the same second
                    n += 1; take = WORK / time.strftime(f"%Y%m%dT%H%M%SZ_{n}_take.wav", time.gmtime())
                take.write_bytes(body)
                if meta:
                    meta["started_at_ms"] = int(q.get("started", 0)) or int(time.time() * 1000)
                device = (self.headers.get("User-Agent") or "unknown client")[:200] + " via PhraseKit web"
                return self.reply(*finish_take(take, meta, q, lang, client, device))
            phrases, names = load_phrases()
            p = phrases.get(action)
            if not p:
                return self.reply(404, {"say": f"unknown phrase {action}"})
            out = speak_phrase(p, lang, names, client)
            out["key"] = p["key"]
            src = q.get("src", "en")
            out["src_text"] = p["text"] if src == "en" else (p["translations"].get(src) or p["text"])
            panel_drafts.log_turn("clinician", kind="phrase", key=p["key"], sourceText=out["src_text"],
                                  translatedText=out["say"], how=out["how"], note=out.get("note"),
                                  repeat=q.get("repeat") == "1")          # "Hear it again" on the patient page
            return self.reply(200, out)
        except subprocess.CalledProcessError as e:
            log("subprocess failed:", e.cmd[0], (e.stderr or b"")[-300:])
            return self.reply(500, {"say": f"{e.cmd[0]} failed"})
        except Exception as e:                      # keep serving; the panel shows the message
            log("error:", repr(e))
            return self.reply(500, {"say": f"error: {e}"})


if __name__ == "__main__":
    threading.Thread(target=beacon, daemon=True).start()
    if not panel_admin.devices():
        log("WARNING: no paired devices (~/panel/devices.json) — every non-public request will get 401; run tools/nano.sh panel")
    tls = panel_admin.tls_ensure()
    if tls:                                                           # HTTPS twin for iPads (mic needs a secure origin)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(tls[0]), str(tls[1]))
        https = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
        https.socket = ctx.wrap_socket(https.socket, server_side=True)
        threading.Thread(target=https.serve_forever, daemon=True).start()
    log(f"panel api on :{PORT}" + (f" and https :{TLS_PORT}" if tls else ""), json.dumps(health()))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
