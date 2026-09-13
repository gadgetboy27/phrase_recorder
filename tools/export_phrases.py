#!/usr/bin/env python3
"""Export the Nano's phrase tables to public/phrases.json — the recorder's list.

    export PG_DSN="postgresql://interpreter_app@nano.local:5432/interpreter_data"
    tools/export_phrases.py            # rewrite public/phrases.json from the DB
    tools/export_phrases.py --check    # exit 1 if the file is out of date, change nothing

Postgres is the source of truth (docs/data-contract.md §2). After adding or
editing rows in `phrases`, `phrase_categories`, or `languages` on the Nano,
run this, then commit and push so the deployed recorder picks it up.
Only `status = 'active'` phrases are exported, each with its approved
translations per target language from golden_set. Stdlib only; needs psql.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "public" / "phrases.json"

# One round trip; Postgres builds the JSON so key order and types are exact.
QUERY = """
SELECT json_build_object(
  'version', 1,
  'categories', (SELECT coalesce(json_agg(json_build_object('slug', value, 'label', label) ORDER BY label), '[]')
                 FROM phrase_categories),
  'languages',  (SELECT coalesce(json_agg(json_build_object('code', code, 'label', label) ORDER BY label), '[]')
                 FROM languages),
  'phrases',    (SELECT coalesce(json_agg(json_build_object(
                    'key', p.phrase_key, 'version', p.version, 'category', p.category, 'text', p.canonical_text,
                    'translations', (SELECT coalesce(json_object_agg(split_part(g.language_pair, '-', 2), g.approved_translation), '{}')
                                     FROM golden_set g
                                     WHERE (g.phrase_key, g.phrase_version) = (p.phrase_key, p.version)
                                       AND g.status = 'approved' AND g.language_pair LIKE 'en-%'))
                  ORDER BY p.phrase_key, p.version), '[]')
                 FROM phrases p WHERE p.status = 'active')
)
"""


def fetch(dsn):
    out = subprocess.run(
        ["psql", dsn, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", QUERY],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def render(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"), help="libpq connection string (env PG_DSN)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check", action="store_true", help="compare only; exit 1 if the file would change")
    a = ap.parse_args()
    if not a.dsn:
        sys.exit("set --dsn or PG_DSN")

    data = fetch(a.dsn)
    new = render(data)
    out = Path(a.out)
    old = out.read_text() if out.exists() else None
    n_tr = sum(len(p["translations"]) for p in data["phrases"])
    summary = f"{len(data['phrases'])} phrases, {n_tr} approved translations, {len(data['categories'])} categories, {len(data['languages'])} languages"

    if old is not None and json.loads(old) == data:
        print(f"{out.relative_to(REPO) if out.is_relative_to(REPO) else out} is up to date ({summary})")
        return
    if a.check:
        print(f"{out} is OUT OF DATE with the database ({summary}) — run tools/export_phrases.py")
        sys.exit(1)
    out.write_text(new)
    print(f"wrote {out} ({summary}) — review with git diff, then commit and push")


if __name__ == "__main__":
    main()
