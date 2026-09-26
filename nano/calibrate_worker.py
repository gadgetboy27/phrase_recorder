#!/usr/bin/env python3
"""Runs ON THE NANO. tools/calibrate.py copies this over and pipes it a job on stdin.

Job (JSON):
  {"lang": "mi", "beams": [4, 2],
   "items": [{"id": "flores:12", "src": "The cat sat on the mat."}, ...]}

Translates every item from English with NLLB-200 at each beam width and hands the
translations back; the Mac scores them against the human reference (tools/calibrate.py).
Nothing is written to the drafts cache or Postgres — this is measurement, not content.

Result (JSON on stdout):
  {"lang": "mi", "rows": [{"id": ..., "src": ..., "b4": "...", "b2": "..."}],
   "timings": {"load_s": 3.0, "beam4_s": 41.2, "beam2_s": 26.8}, "empty": {"b4": 0, "b2": 0}}
Progress goes to stderr. Stdlib only — panel_drafts brings ctranslate2/sentencepiece.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_drafts  # noqa: E402


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    job = json.load(sys.stdin)
    lang, items = job["lang"], job["items"]
    beams = job.get("beams", [4, 2])
    if not panel_drafts.nllb_available():
        json.dump({"error": "NLLB not installed on the Nano"}, sys.stdout)
        return
    t0 = time.time()
    panel_drafts.nllb_load()
    timings = {"load_s": round(time.time() - t0, 2)}
    rows = [{"id": it["id"], "src": it["src"]} for it in items]
    empty = {}
    for beam in beams:
        key = f"b{beam}"
        t0 = time.time()
        n_empty = 0
        for i, it in enumerate(items):
            # nllb_translate returns None when NLLB echoes the source or emits ⁇ — i.e. it could not
            # translate this line. That is a result, not an error: it is how Tongan behaves throughout.
            out = panel_drafts.nllb_translate(it["src"], "en", lang, beam=beam)
            rows[i][key] = out
            n_empty += out is None
            if (i + 1) % 25 == 0:
                log(f"  {lang} beam{beam}: {i + 1}/{len(items)}")
        timings[f"beam{beam}_s"] = round(time.time() - t0, 2)
        empty[key] = n_empty
        log(f"  {lang} beam{beam}: {len(items)} lines in {timings[f'beam{beam}_s']} s, {n_empty} untranslatable")
    json.dump({"lang": lang, "rows": rows, "timings": timings, "empty": empty}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
