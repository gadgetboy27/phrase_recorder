#!/usr/bin/env python3
"""Add a phrase to the Nano's `phrases` table and refresh public/phrases.json.

    tools/add_phrase.py --category intake "What is your date of birth?"
    tools/add_phrase.py --revise p003 "Please keep as still as you can for me."
    tools/add_phrase.py --categories          # list category slugs

--category adds a new phrase with the next free key (p011, p012, …).
--revise adds a new *version* of an existing key and marks the old version
superseded; recordings of the old version keep pointing at it (that's the
point of versioning). Then commit and push public/phrases.json so the
deployed recorder shows the change. Stdlib only; needs psql.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_phrases  # noqa: E402


def psql(dsn, sql):
    return subprocess.run(
        ["psql", dsn, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def q(v):
    return "'" + str(v).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="the phrase, in English, exactly as it should be read")
    ap.add_argument("--category", help="category slug for a new phrase")
    ap.add_argument("--revise", metavar="KEY", help="add a new version of this phrase key (e.g. p003)")
    ap.add_argument("--categories", action="store_true", help="list category slugs and exit")
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"))
    a = ap.parse_args()
    if not a.dsn:
        sys.exit("set --dsn or PG_DSN")

    if a.categories:
        print(psql(a.dsn, "SELECT value || '  ' || label FROM phrase_categories ORDER BY value"))
        return
    text = (a.text or "").strip()
    if not text:
        sys.exit("give the phrase text")
    if bool(a.category) == bool(a.revise):
        sys.exit("use exactly one of --category or --revise")

    if a.category:
        cats = psql(a.dsn, "SELECT value FROM phrase_categories").split("\n")
        if a.category not in cats:
            sys.exit(f"unknown category {a.category!r}; one of: {', '.join(sorted(cats))}")
        dup = psql(a.dsn, f"SELECT phrase_key||'v'||version FROM phrases WHERE status='active' AND lower(canonical_text) = lower({q(text)})")
        if dup:
            sys.exit(f"that text already exists as {dup}")
        last = psql(a.dsn, r"SELECT coalesce(max(substring(phrase_key from '^p(\d+)$')::int), 0) FROM phrases")
        key = f"p{int(last) + 1:03d}"
        psql(a.dsn, f"INSERT INTO phrases (phrase_key, version, category, canonical_text) VALUES ({q(key)}, 1, {q(a.category)}, {q(text)})")
        print(f"added {key}v1 [{a.category}]: {text}")
    else:
        key = a.revise.strip().lower()
        if not re.match(r"^p\d{3,}$", key):
            sys.exit("--revise expects a key like p003")
        cur = psql(a.dsn, f"SELECT version||'|'||category||'|'||canonical_text FROM phrases WHERE phrase_key={q(key)} AND status='active' ORDER BY version DESC LIMIT 1")
        if not cur:
            sys.exit(f"no active phrase with key {key}")
        ver, cat, old = cur.split("|", 2)
        new_ver = int(ver) + 1
        psql(a.dsn,
             f"BEGIN; UPDATE phrases SET status='superseded' WHERE phrase_key={q(key)} AND version={ver}; "
             f"INSERT INTO phrases (phrase_key, version, category, canonical_text) VALUES ({q(key)}, {new_ver}, {q(cat)}, {q(text)}); COMMIT;")
        print(f"{key}v{ver} superseded → {key}v{new_ver}: {text}\n  (was: {old})")

    sys.argv = ["export_phrases.py", "--dsn", a.dsn]
    export_phrases.main()


if __name__ == "__main__":
    main()
