#!/usr/bin/env python3
"""Review translations volunteers typed into the recorder (draft golden_set rows).

    tools/translations.py list               # drafts awaiting review, with their recordings
    tools/translations.py list --all         # every translation, any status
    tools/translations.py play 12            # play the recording(s) for golden_set row 12 on the Nano's USB audio
    tools/translations.py approve 12         # mark approved (supersedes any earlier approved one)
    tools/translations.py reject 12
    tools/translations.py set p003 mi "Kia āta noho koe mōku."   # enter/approve a translation directly

After approving, run tools/export_phrases.py (and commit/push) so the
recorder shows the approved text to the next volunteer. Stdlib only; needs
psql; `play` needs SSH access to the Nano (env NANO_SSH, e.g. gadgetboy@192.168.68.111,
and NANO_DEST's path for the files).
"""
import argparse
import os
import subprocess
import sys


def psql(dsn, sql):
    return subprocess.run(
        ["psql", dsn, "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout.rstrip("\n")


def q(v):
    return "'" + str(v).replace("'", "''") + "'"


def cmd_list(a):
    where = "" if a.all else "WHERE g.status = 'draft'"
    out = psql(a.dsn, f"""
        SELECT g.id, g.status, g.phrase_key||'v'||g.phrase_version, g.language_pair, g.created_by,
               g.approved_translation, p.canonical_text,
               (SELECT count(*) FROM audio_samples s WHERE s.linked_golden_set_id = g.id)
        FROM golden_set g LEFT JOIN phrases p ON (p.phrase_key, p.version) = (g.phrase_key, g.phrase_version)
        {where} ORDER BY g.status = 'draft' DESC, g.phrase_key, g.language_pair, g.id""")
    if not out:
        print("no drafts awaiting review" if not a.all else "no translations yet")
        return
    for line in out.split("\n"):
        gid, status, key, pair, by, tr, en, n = line.split("\t")
        print(f"#{gid:<4} {status:<10} {key} {pair}  by {by or '?'}  ({n} recording{'s' if n != '1' else ''})")
        print(f"       EN: {en}")
        print(f"       {pair.split('-')[1].upper()}: {tr}\n")


def cmd_play(a):
    ssh = os.environ.get("NANO_SSH")
    dest = os.environ.get("NANO_DEST", "")
    if not ssh:
        sys.exit("set NANO_SSH (e.g. gadgetboy@192.168.68.111)")
    path = dest.split(":", 1)[1] if ":" in dest else "~/phrase-recordings"
    files = psql(a.dsn, f"SELECT file_ref FROM audio_samples WHERE linked_golden_set_id = {int(a.id)} ORDER BY id")
    if not files:
        sys.exit(f"no recordings linked to golden_set #{a.id}")
    for f in files.split("\n"):
        print(f"playing {f}")
        subprocess.run(["ssh", ssh, f"aplay -q -D plughw:0,0 {path}/{f} || (sleep 2 && aplay -q -D plughw:0,0 {path}/{f})"], check=False)


def cmd_approve(a):
    gid = int(a.id)
    row = psql(a.dsn, f"SELECT phrase_key||'|'||phrase_version||'|'||language_pair||'|'||status FROM golden_set WHERE id={gid}")
    if not row:
        sys.exit(f"no golden_set row #{gid}")
    key, ver, pair, status = row.split("|")
    psql(a.dsn, f"""BEGIN;
        UPDATE golden_set SET status='superseded', superseded_by={gid}
          WHERE phrase_key={q(key)} AND phrase_version={ver} AND language_pair={q(pair)} AND status='approved' AND id<>{gid};
        UPDATE golden_set SET status='approved' WHERE id={gid};
        COMMIT;""")
    print(f"#{gid} approved for {key}v{ver} {pair} (was {status}). Now: tools/export_phrases.py && git commit && git push")


def cmd_reject(a):
    psql(a.dsn, f"UPDATE golden_set SET status='rejected' WHERE id={int(a.id)} AND status <> 'approved'")
    print(f"#{a.id} rejected (approved rows can't be rejected — approve a replacement instead)")


def cmd_set(a):
    key, lang, text = a.key.lower(), a.lang.lower(), a.text.strip()
    cur = psql(a.dsn, f"SELECT version||'|'||category||'|'||canonical_text FROM phrases WHERE phrase_key={q(key)} AND status='active' ORDER BY version DESC LIMIT 1")
    if not cur:
        sys.exit(f"no active phrase {key}")
    ver, cat, en = cur.split("|", 2)
    pair = f"en-{lang}"
    gid = psql(a.dsn, f"""
        INSERT INTO golden_set (language_pair, source_text, approved_translation, category, phrase_key, phrase_version, status, created_by)
        VALUES ({q(pair)}, {q(en)}, {q(text)}, {q(cat)}, {q(key)}, {ver}, 'draft', 'reviewer')
        ON CONFLICT (phrase_key, phrase_version, language_pair, approved_translation) DO UPDATE SET status = golden_set.status
        RETURNING id""")
    a.id = gid.split("\n")[0]          # psql appends the INSERT tag after RETURNING
    cmd_approve(a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("play"); s.add_argument("id"); s.set_defaults(fn=cmd_play)
    s = sub.add_parser("approve"); s.add_argument("id"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("reject"); s.add_argument("id"); s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("set"); s.add_argument("key"); s.add_argument("lang"); s.add_argument("text"); s.set_defaults(fn=cmd_set)
    a = ap.parse_args()
    if not a.dsn:
        sys.exit("set --dsn or PG_DSN")
    a.fn(a)


if __name__ == "__main__":
    main()
