#!/usr/bin/env python3
"""Score the Nano's model stack against the golden set. The bake-off number (brief §4).

    tools/bench.py                      # every approved golden_set row that has recordings
    tools/bench.py --lang mi            # one language
    tools/bench.py --no-translate       # ASR only (skip the LLM)
    tools/bench.py --model ggml-base.en.bin --force   # another Whisper model; re-trim derived files

What it does, per approved translation:
  ASR   each linked recording is trimmed to its speech (Silero VAD) and resampled to
        16 kHz on the Nano — the original WAV is untouched — then transcribed by
        whisper-cli. Scored against the approved translation text: CER and WER,
        plus the same without macrons, so "whakaha" vs "whakahā" is visible as a
        diacritic miss rather than a wrong word.
  LLM   the English is sent to the llama-server on the Nano and scored the same way.

Writes ~/phrase-recordings/bench/<timestamp>.json (full report) and records the
derived file + speech boundaries on each audio_samples row (migration 003).
Needs psql, rsync, and SSH to the Nano (NANO_SSH, NANO_DEST, PG_DSN as for push.py).
Stdlib only.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = HERE.parent / "nano" / "asr_worker.py"
DEFAULT_ROOT = Path.home() / "phrase-recordings"


def psql(dsn, sql):
    return subprocess.run(
        ["psql", dsn, "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout.rstrip("\n")


def q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


# ---- scoring -------------------------------------------------------------

def norm(s, strip_macrons=False):
    s = s.lower()
    if strip_macrons:
        s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def score(ref, hyp):
    """{'cer','wer','cer_nomacron','wer_nomacron'} as percentages; hyp None → all None."""
    if hyp is None:
        return {k: None for k in ("cer", "wer", "cer_nomacron", "wer_nomacron")}
    out = {}
    for suffix, sm in (("", False), ("_nomacron", True)):
        r, h = norm(ref, sm), norm(hyp, sm)
        out["cer" + suffix] = round(100 * edit_distance(r, h) / max(1, len(r)), 1)
        out["wer" + suffix] = round(100 * edit_distance(r.split(), h.split()) / max(1, len(r.split())), 1)
    return out


# ---- main ----------------------------------------------------------------

def load_golden(dsn, lang):
    where = f"AND g.language_pair = 'en-{lang}'" if lang else ""
    rows = psql(dsn, f"""
        SELECT g.id, g.language_pair, g.phrase_key||'v'||g.phrase_version, g.source_text, g.approved_translation,
               l.label, coalesce(string_agg(s.id||'|'||s.file_ref||'|'||coalesce(s.speaker_type,'')||'|'||coalesce(s.speaker_id,''), E'\\x1f' ORDER BY s.id), '')
        FROM golden_set g
        JOIN languages l ON l.code = split_part(g.language_pair, '-', 2)
        LEFT JOIN audio_samples s ON (s.phrase_key, s.phrase_version) = (g.phrase_key, g.phrase_version)
                                  AND s.language = split_part(g.language_pair, '-', 2)
        WHERE g.status = 'approved' AND g.approved_translation IS NOT NULL {where}
        GROUP BY g.id, l.label ORDER BY g.language_pair, g.phrase_key""")
    golden = []
    for line in rows.split("\n") if rows else []:
        gid, pair, key, en, tr, label, recs = line.split("\t")
        golden.append({
            "id": int(gid), "pair": pair, "key": key, "source": en, "approved": tr, "language_label": label,
            "recordings": [dict(zip(("sample_id", "file_ref", "speaker_type", "speaker_id"), r.split("|")))
                           for r in recs.split("\x1f") if r],
        })
    return golden


def run_worker(ssh, nano_root, job):
    subprocess.run(["rsync", "-a", str(WORKER), f"{ssh}:{nano_root}/.bench/"], check=True)
    p = subprocess.run(["ssh", ssh, f"python3 {nano_root}/.bench/asr_worker.py"],
                       input=json.dumps(job), capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    if p.returncode:
        sys.exit(f"worker failed on the Nano (exit {p.returncode})")
    return json.loads(p.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", help="language code, e.g. mi (default: all)")
    ap.add_argument("--model", default="ggml-large-v3-turbo-q5_0.bin", help="file under ~/whisper.cpp/models on the Nano")
    ap.add_argument("--no-translate", action="store_true", help="skip the LLM")
    ap.add_argument("--force", action="store_true", help="re-trim derived files even if they exist")
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"))
    ap.add_argument("--ssh", default=os.environ.get("NANO_SSH"))
    ap.add_argument("--dest", default=os.environ.get("NANO_DEST"))
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Mac side; report goes under <root>/bench")
    a = ap.parse_args()
    for name in ("dsn", "ssh", "dest"):
        if not getattr(a, name):
            sys.exit(f"set --{name} or {'PG_DSN' if name == 'dsn' else 'NANO_' + name.upper()}")
    nano_root = a.dest.split(":", 1)[1] if ":" in a.dest else a.dest

    golden = load_golden(a.dsn, a.lang)
    if not golden:
        sys.exit("no approved translations to score" + (f" for {a.lang}" if a.lang else ""))

    job = {"root": nano_root, "whisper_model": a.model, "force": a.force,
           "files": [{"file_ref": r["file_ref"], "language": g["pair"].split("-")[1]} for g in golden for r in g["recordings"]],
           "translate": [] if a.no_translate else [{"id": g["id"], "source": g["source"], "language": g["language_label"]} for g in golden]}
    print(f"{len(golden)} approved translation(s), {len(job['files'])} recording(s), whisper {a.model}"
          + (", LLM off" if a.no_translate else "") + " — running on the Nano…", flush=True)
    res = run_worker(a.ssh, nano_root, job)
    by_file = {f["file_ref"]: f for f in res["files"]}
    by_gid = {t["id"]: t["llm_text"] for t in res["translations"]}

    # ---- report ----
    updates, asr_scores, llm_scores = [], [], []
    saved_bytes = 0
    for g in golden:
        print(f"\n{g['key']} {g['pair']}  EN: {g['source']}")
        print(f"   approved : {g['approved']}")
        for r in g["recordings"]:
            f = by_file.get(r["file_ref"], {})
            r.update(f)
            if f.get("error"):
                print(f"   ASR {r['speaker_id'] or '?'} ({r['speaker_type']}): !! {f['error']}")
                continue
            r["score"] = score(g["approved"], f["asr_text"])
            asr_scores.append(r["score"])
            saved_bytes += f["original_bytes"] - f["derived_bytes"]
            s = r["score"]
            print(f"   ASR {r['speaker_id'] or '?'} ({r['speaker_type']}): {f['asr_text']}")
            print(f"             CER {s['cer']:5.1f}%  WER {s['wer']:5.1f}%   no-macron CER {s['cer_nomacron']:5.1f}%  WER {s['wer_nomacron']:5.1f}%"
                  f"   speech {f['speech_start_ms']/1000:.2f}–{f['speech_end_ms']/1000:.2f}s, {f['original_bytes']//1024} KB → {f['derived_bytes']//1024} KB")
            updates.append(f"UPDATE audio_samples SET derived_ref={q(f['derived_ref'])}, speech_start_ms={f['speech_start_ms']}, "
                           f"speech_end_ms={f['speech_end_ms']} WHERE id={int(r['sample_id'])};")
        if not a.no_translate:
            g["llm_text"] = by_gid.get(g["id"])
            g["llm_score"] = score(g["approved"], g["llm_text"])
            if g["llm_text"] is None:
                print("   LLM      : !! no answer (is llama-server running on the Nano?)")
            else:
                llm_scores.append(g["llm_score"])
                s = g["llm_score"]
                print(f"   LLM      : {g['llm_text']}")
                print(f"             CER {s['cer']:5.1f}%  WER {s['wer']:5.1f}%   no-macron CER {s['cer_nomacron']:5.1f}%  WER {s['wer_nomacron']:5.1f}%")

    def mean(scores, k):
        v = [s[k] for s in scores if s[k] is not None]
        return f"{sum(v)/len(v):5.1f}%" if v else "  n/a"

    print(f"\n== ASR ({a.model}) over {len(asr_scores)} recording(s):"
          f"  CER {mean(asr_scores,'cer')}  WER {mean(asr_scores,'wer')}   no-macron CER {mean(asr_scores,'cer_nomacron')}  WER {mean(asr_scores,'wer_nomacron')}")
    if llm_scores:
        print(f"== LLM over {len(llm_scores)} phrase(s):"
              f"  CER {mean(llm_scores,'cer')}  WER {mean(llm_scores,'wer')}   no-macron CER {mean(llm_scores,'cer_nomacron')}  WER {mean(llm_scores,'wer_nomacron')}")
    t = res["timings"]
    print(f"== Nano time: VAD+trim {t['vad_derive_s']}s, ASR {t['asr_s']}s, LLM {t['llm_s']}s.  Derived copies save {saved_bytes//1024} KB.")

    if updates:
        psql(a.dsn, "BEGIN;\n" + "\n".join(updates) + "\nCOMMIT;")
    out_dir = a.root / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    report.write_text(json.dumps({"whisper_model": a.model, "lang": a.lang, "timings": t, "golden": golden},
                                 ensure_ascii=False, indent=2))
    print(f"report: {report}")


if __name__ == "__main__":
    main()
