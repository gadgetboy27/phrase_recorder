#!/usr/bin/env python3
"""Push staged batches from the Mac to the Nano: rsync the files, insert the rows.

    export NANO_DEST=nano@nano.local:/srv/phrase-recordings
    export PG_DSN="postgresql://interpreter_app:PASSWORD@nano.local:5432/interpreter_data"

    tools/push.py --all                 # every batch under ~/phrase-recordings/staged
    tools/push.py 20260913T101500Z_mg_es_a1b2c3
    tools/push.py --all --dry-run       # show the rsync and SQL, change nothing

For each batch (docs/data-contract.md §8):
  1. rsync  staged/<batch_id>/  →  $NANO_DEST/<batch_id>/   (files, verbatim)
  2. INSERT one audio_samples row per recording, ON CONFLICT (sha256) DO NOTHING,
     so re-pushing is harmless. A claimed phrase key that isn't in the Nano's
     `phrases` table is filed with phrase_key NULL + unmatched_phrase_text and
     printed — never guessed.
  3. move staged/<batch_id>  →  pushed/<batch_id>

Needs `psql` and `rsync` on PATH (brew install libpq rsync). Stdlib only.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / "phrase-recordings"


def sql_str(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return sql_str(v)


def psql(dsn, sql, *extra):
    return subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", *extra, "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout


def known_phrases(dsn):
    out = psql(dsn, "SELECT phrase_key || '|' || version FROM phrases")
    return {tuple(line.split("|")) for line in out.split("\n") if line}


def build_insert(m, batch_id, known):
    """Returns (sql, unmatched_notes). One multi-row INSERT inside a transaction."""
    sp = m["speaker"]
    device = m.get("device", {}).get("user_agent")
    rows, notes = [], []
    for r in m["recordings"]:
        key, ver = r.get("phrase_key"), r.get("phrase_version")
        note = None
        if r.get("unmatched"):
            key = ver = None
        elif known is not None and (str(key), str(ver)) not in known:
            note = f"claimed {key}v{ver} not in phrases"
            notes.append(f"{r['file']}: {note}")
            key = ver = None
        unmatched_text = r["phrase_text"] if key is None else None
        rows.append("(" + ", ".join([
            sql_str(m["language"]),
            sql_str(sp["type"]),
            sql_str(r["purpose"]),
            sql_str(f"{batch_id}/{r['file']}"),
            "false",                                   # transcript_confirmed: QC happens later
            sql_str(key), sql_val(ver), sql_str(unmatched_text),
            sql_str(r["category"]),
            sql_str(sp["id"]),
            sql_val(r["take"]),
            sql_val(r["sample_rate"]), sql_val(r["duration_ms"]),
            sql_str(r["sha256"]),
            sql_str(r["recorded_at"]),
            sql_str(device),
            sql_val(bool(sp.get("consent_confirmed"))),
            sql_str(batch_id),
            sql_str(note),
        ]) + ")")
    sql = (
        "BEGIN;\n"
        "INSERT INTO audio_samples (language, speaker_type, purpose, file_ref, transcript_confirmed,\n"
        "  phrase_key, phrase_version, unmatched_phrase_text, category, speaker_id, take,\n"
        "  sample_rate, duration_ms, sha256, recorded_at, device, consent_confirmed, batch_id, notes)\n"
        "VALUES\n  " + ",\n  ".join(rows) + "\n"
        "ON CONFLICT (sha256) DO NOTHING\n"
        "RETURNING id;\n"
        "COMMIT;\n"
    )
    return sql, notes


def push_batch(batch_dir, root, dest, dsn, dry_run):
    batch_id = batch_dir.name
    m = json.loads((batch_dir / "manifest.json").read_text())
    print(f"\n== {batch_id}  ({len(m['recordings'])} recordings, {m['language']} / {m['speaker']['id']})")

    rsync = ["rsync", "-a", "--checksum", "--exclude", "derived/", str(batch_dir) + "/", f"{dest.rstrip('/')}/{batch_id}/"]
    known = known_phrases(dsn) if dsn else None
    sql, notes = build_insert(m, batch_id, known)
    for n in notes:
        print(f"   ! {n}")
    if known is None:
        print("   (no PG_DSN: phrase keys not checked against the Nano)")

    if dry_run:
        print("   rsync:", " ".join(rsync))
        print("   sql:\n" + "\n".join("     " + l for l in sql.splitlines()))
        return True

    print("   rsync →", f"{dest}/{batch_id}/")
    subprocess.run(rsync, check=True)

    out = psql(dsn, sql)
    inserted = sum(1 for l in out.split("\n") if l.strip().isdigit())
    skipped = len(m["recordings"]) - inserted
    print(f"   rows: {inserted} inserted, {skipped} already present (same sha256)")

    pushed = root / "pushed" / batch_id
    if pushed.exists():
        shutil.rmtree(pushed)
    shutil.move(str(batch_dir), str(pushed))
    print(f"   moved → {pushed}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batches", nargs="*", help="batch ids under staged/ (or --all)")
    ap.add_argument("--all", action="store_true", help="push every staged batch")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--dest", default=os.environ.get("NANO_DEST"), help="rsync destination, e.g. nano@nano.local:/srv/phrase-recordings (env NANO_DEST)")
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"), help="libpq connection string for the Nano's Postgres (env PG_DSN)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.dest:
        sys.exit("set --dest or NANO_DEST")
    if not a.dsn and not a.dry_run:
        sys.exit("set --dsn or PG_DSN")
    for tool in ("rsync",) + (("psql",) if a.dsn else ()):
        if not shutil.which(tool):
            sys.exit(f"{tool} not on PATH")

    root = Path(a.root).expanduser()
    staged = root / "staged"
    if a.all:
        dirs = sorted(p for p in staged.iterdir() if (p / "manifest.json").exists())
    else:
        dirs = [staged / b for b in a.batches]
        missing = [d for d in dirs if not (d / "manifest.json").exists()]
        if missing:
            sys.exit("not staged: " + ", ".join(d.name for d in missing))
    if not dirs:
        sys.exit("nothing staged")

    for d in dirs:
        push_batch(d, root, a.dest, a.dsn, a.dry_run)
    print(f"\n{len(dirs)} batch(es) {'previewed' if a.dry_run else 'pushed'}")


if __name__ == "__main__":
    main()
