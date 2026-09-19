#!/usr/bin/env python3
"""Runs ON THE NANO. tools/bench.py copies this over and pipes it a job on stdin.

Job (JSON):
  {"root": "/home/gadgetboy/phrase-recordings",
   "whisper_model": "ggml-large-v3-turbo-q5_0.bin",
   "force": false,
   "files": [{"file_ref": "<batch>/<file>.wav", "language": "mi"}, ...],
   "translate": [{"id": 12, "source": "Try to breathe slowly and deeply.", "language": "Te Reo Māori"}, ...]}

For every file:
  1. Silero VAD (whisper-vad-speech-segments) finds where speech starts and
     ends in the ORIGINAL recording.
  2. sox cuts that span (+ padding) and resamples to 16 kHz mono into
     <batch>/derived/16k-trim/<file>. The original is never modified.
     Skipped when the derived file already exists unless "force".
  3. whisper-cli transcribes the derived file (one process for all files,
     so the model loads once).
For every translate item: ask the llama-server already running on :8080.

Result (JSON on stdout): {"files": [...per file...], "translations": [...], "timings": {...}}
Progress goes to stderr. Stdlib only — nothing to install on the Nano.
"""
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

WHISPER = Path.home() / "whisper.cpp"
VAD_MODEL = WHISPER / "models" / "ggml-silero-v6.2.0.bin"
LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
# Whisper's language ids don't line up with ours everywhere: Dari is decoded as
# Farsi; Tongan and Samoan aren't in Whisper at all (no drafts, no scores —
# those need the fine-tuning route or a different ASR).
WHISPER_LANG = {"prs": "fa"}
WHISPER_UNSUPPORTED = {"to": "Tongan", "sm": "Samoan"}
PAD_MS = 300            # keep room either side of the VAD boundaries so a soft first consonant survives
DERIVED_SUBDIR = "derived/16k-trim"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def vad_bounds(wav):
    """(start_ms, end_ms) of speech in the original file, or None if VAD hears nothing."""
    out = subprocess.run(
        [str(WHISPER / "build/bin/whisper-vad-speech-segments"), "-vm", str(VAD_MODEL), "-np", "-f", str(wav)],
        capture_output=True, text=True,
    ).stdout
    # "Speech segment 0: start = 122.00, end = 189.00"  (units: 10 ms)
    segs = [(float(s), float(e)) for s, e in re.findall(r"start = ([\d.]+), end = ([\d.]+)", out)]
    if not segs:
        return None
    return int(segs[0][0] * 10), int(segs[-1][1] * 10)


def duration_ms(wav):
    return int(float(subprocess.run(["sox", "--i", "-D", str(wav)], capture_output=True, text=True).stdout) * 1000)


def derive(src, dst, start_ms, end_ms):
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["sox", str(src), "-r", "16000", "-c", "1", "-b", "16", str(dst),
         "trim", f"{start_ms / 1000:.3f}", f"={end_ms / 1000:.3f}"],
        check=True, capture_output=True,
    )


def transcribe(model, language, wavs):
    """whisper-cli over all files at once; returns {wav: text}. Writes then removes <wav>.json sidecars."""
    if not wavs:
        return {}
    cmd = [str(WHISPER / "build/bin/whisper-cli"), "-m", str(WHISPER / "models" / model),
           "-l", language, "-np", "-nt", "-oj"]
    for w in wavs:
        cmd += ["-f", str(w)]
    subprocess.run(cmd, check=True, capture_output=True)
    texts = {}
    for w in wavs:
        side = Path(str(w) + ".json")
        d = json.loads(side.read_text())
        texts[str(w)] = " ".join(s["text"].strip() for s in d["transcription"]).strip()
        side.unlink()
    return texts


def translate(source, language):
    body = {
        "messages": [
            {"role": "system", "content":
                f"You are a medical interpreter. Translate the clinician's English sentence into {language} "
                "exactly as it would be said to a patient in an emergency. Reply with the translation only — "
                "no explanation, no quotes, no English."},
            {"role": "user", "content": source},
        ],
        "temperature": 0, "max_tokens": 120,
    }
    req = urllib.request.Request(LLAMA_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def main():
    job = json.load(sys.stdin)
    root = Path(job["root"])
    t = {}
    result = {"files": [], "translations": [], "timings": t}

    # 1+2: VAD + derive
    t0 = time.time()
    by_lang = {}
    for item in job["files"]:
        src = root / item["file_ref"]
        rec = {"file_ref": item["file_ref"]}
        result["files"].append(rec)
        if not src.exists():
            rec["error"] = "missing on Nano"
            continue
        if item["language"] in WHISPER_UNSUPPORTED:
            rec["error"] = f"Whisper has no {WHISPER_UNSUPPORTED[item['language']]} model — no transcript"
            continue
        batch, name = item["file_ref"].split("/", 1)
        dst = root / batch / DERIVED_SUBDIR / name
        rec["derived_ref"] = f"{batch}/{DERIVED_SUBDIR}/{name}"
        b = vad_bounds(src)
        if b is None:
            rec["error"] = "VAD found no speech"
            continue
        total = duration_ms(src)
        rec["speech_start_ms"], rec["speech_end_ms"] = b
        s, e = max(0, b[0] - PAD_MS), min(total, b[1] + PAD_MS)
        if job.get("force") or not dst.exists():
            derive(src, dst, s, e)
        rec["original_bytes"], rec["derived_bytes"] = src.stat().st_size, dst.stat().st_size
        rec["original_ms"], rec["derived_ms"] = total, e - s
        by_lang.setdefault(item["language"], []).append((rec, dst))
        log(f"  vad {name[:40]:40} {b[0]/1000:5.2f}–{b[1]/1000:5.2f}s of {total/1000:.2f}s")
    t["vad_derive_s"] = round(time.time() - t0, 2)

    # 3: ASR, one whisper process per language
    t0 = time.time()
    for lang, items in by_lang.items():
        texts = transcribe(job["whisper_model"], WHISPER_LANG.get(lang, lang), [dst for _, dst in items])
        for rec, dst in items:
            rec["asr_text"] = texts[str(dst)]
    t["asr_s"] = round(time.time() - t0, 2)

    # 4: translation via the running llama-server
    t0 = time.time()
    for item in job.get("translate", []):
        try:
            out = translate(item["source"], item["language"])
        except Exception as ex:                     # server down → report, don't abort the ASR results
            out = None
            log(f"  llm {item['id']}: {ex}")
        result["translations"].append({"id": item["id"], "llm_text": out})
    t["llm_s"] = round(time.time() - t0, 2)

    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
