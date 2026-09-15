#!/usr/bin/env python3
"""Let Whisper draft the transcript of recordings that have no translation text yet.

    tools/draft_transcripts.py --lang fa          # every fa recording whose phrase has no approved fa text
    tools/draft_transcripts.py --lang fa --all    # even phrases that already have an approved translation
    tools/draft_transcripts.py --lang fa --dry-run

A volunteer who records without typing the text leaves the phrase with audio
but no target-language text — so it can't join the golden set and can't be
scored. This runs each such recording through Whisper on the Nano (trimmed
16 kHz derived copy, as bench.py does) and files the result as a *draft*
golden_set row, created_by 'whisper:<model>', linked to the recording.

A draft is a suggestion for a human who reads the language, nothing more:
review with `tools/translations.py list`, `play <id>`, then approve / reject,
exactly as for typed translations. Nothing here is ever auto-approved.
Identical drafts collapse; re-running is harmless.

Needs the same env as bench.py (PG_DSN, NANO_SSH, NANO_DEST). Stdlib only.
"""
import argparse
import os
import sys

from bench import psql, q, run_worker


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", required=True, help="language code, e.g. fa")
    ap.add_argument("--all", action="store_true", help="include phrases that already have an approved translation")
    ap.add_argument("--model", default="ggml-large-v3-turbo-q5_0.bin")
    ap.add_argument("--dry-run", action="store_true", help="transcribe and print, write nothing")
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"))
    ap.add_argument("--ssh", default=os.environ.get("NANO_SSH"))
    ap.add_argument("--dest", default=os.environ.get("NANO_DEST"))
    a = ap.parse_args()
    for name in ("dsn", "ssh", "dest"):
        if not getattr(a, name):
            sys.exit(f"set --{name} or {'PG_DSN' if name == 'dsn' else 'NANO_' + name.upper()}")
    nano_root = a.dest.split(":", 1)[1] if ":" in a.dest else a.dest
    lang, pair = a.lang.lower(), f"en-{a.lang.lower()}"

    skip_approved = "" if a.all else f"""
        AND NOT EXISTS (SELECT 1 FROM golden_set g WHERE (g.phrase_key, g.phrase_version) = (s.phrase_key, s.phrase_version)
                        AND g.language_pair = {q(pair)} AND g.status = 'approved')"""
    rows = psql(a.dsn, f"""
        SELECT s.id, s.file_ref, s.phrase_key, s.phrase_version, p.category, p.canonical_text, s.speaker_id, s.take
        FROM audio_samples s JOIN phrases p ON (p.phrase_key, p.version) = (s.phrase_key, s.phrase_version)
        WHERE s.language = {q(lang)} AND s.phrase_key IS NOT NULL {skip_approved}
        ORDER BY s.phrase_key, s.take, s.id""")
    if not rows:
        sys.exit(f"nothing to draft for {lang}" + ("" if a.all else " (every recorded phrase already has an approved translation; --all to draft anyway)"))
    recs = [dict(zip(("sample_id", "file_ref", "key", "ver", "category", "en", "speaker", "take"), r.split("\t")))
            for r in rows.split("\n")]

    print(f"{len(recs)} {lang} recording(s) → whisper {a.model} on the Nano…", flush=True)
    res = run_worker(a.ssh, nano_root, {"root": nano_root, "whisper_model": a.model, "force": False,
                                        "files": [{"file_ref": r["file_ref"], "language": lang} for r in recs],
                                        "translate": []})
    by_file = {f["file_ref"]: f for f in res["files"]}

    sql, n = [], 0
    for r in recs:
        f = by_file.get(r["file_ref"], {})
        print(f"\n{r['key']}v{r['ver']} take {r['take']} ({r['speaker']})  EN: {r['en']}")
        if f.get("error") or not f.get("asr_text"):
            print(f"   !! {f.get('error') or 'empty transcript'} — no draft")
            continue
        print(f"   {lang.upper()}: {f['asr_text']}")
        n += 1
        sql.append(f"""
            WITH g AS (
              INSERT INTO golden_set (language_pair, source_text, approved_translation, category, phrase_key, phrase_version, status, created_by)
              VALUES ({q(pair)}, {q(r['en'])}, {q(f['asr_text'])}, {q(r['category'])}, {q(r['key'])}, {r['ver']}, 'draft', {q('whisper:' + a.model)})
              ON CONFLICT (phrase_key, phrase_version, language_pair, approved_translation) DO UPDATE SET status = golden_set.status
              RETURNING id)
            UPDATE audio_samples s SET linked_golden_set_id = coalesce(s.linked_golden_set_id, g.id),
              derived_ref = {q(f['derived_ref'])}, speech_start_ms = {f['speech_start_ms']}, speech_end_ms = {f['speech_end_ms']}
            FROM g WHERE s.id = {int(r['sample_id'])};""")

    if a.dry_run:
        print(f"\n(dry run) {n} draft(s) not written")
        return
    if sql:
        psql(a.dsn, "BEGIN;" + "".join(sql) + "\nCOMMIT;")
    print(f"\n{n} draft(s) filed for review: tools/translations.py list")


if __name__ == "__main__":
    main()
