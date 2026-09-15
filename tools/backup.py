#!/usr/bin/env python3
"""Back the Nano up to the Mac: pg_dump of interpreter_data + mirror of the recordings.

    tools/backup.py              # run once, now
    tools/backup.py --install    # also run every day at 21:00 via launchd (re-run to update)
    tools/backup.py --uninstall

Writes under ~/phrase-recordings/backups/:
    db/interpreter_data_<UTC>.sql.gz   plain-SQL dump, gzip'd; last 30 kept
    nano/                              rsync mirror of $NANO_DEST (originals + derived/)
    backup.log                         one line per run

The database is the golden set, corrections, languages, phrase list —
everything the brief says must stay self-hosted. It lives on the Nano's SD
card, so this is the only other copy. If the Nano is off the run is skipped
(logged, exit 0) so launchd doesn't complain every night.

Restore: createdb interpreter_data && gunzip -c db/<file>.sql.gz | psql interpreter_data
Needs pg_dump/psql (brew libpq) and rsync; PG_DSN, NANO_SSH, NANO_DEST as for push.py;
password in ~/.pgpass. Stdlib only.
"""
import argparse
import datetime as dt
import gzip
import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "phrase-recordings" / "backups"
KEEP = 30
LABEL = "nz.phrase-recorder.backup"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LIBPQ = "/usr/local/opt/libpq/bin"       # brew libpq isn't linked into PATH for launchd


def log(msg):
    ROOT.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(ROOT / "backup.log", "a") as f:
        f.write(line + "\n")


def nano_up(ssh):
    host = ssh.split("@")[-1]
    try:
        socket.create_connection((host, 22), timeout=5).close()
        return True
    except OSError:
        return False


def dump_db(dsn):
    out_dir = ROOT / "db"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"interpreter_data_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sql.gz"
    p = subprocess.run(["pg_dump", dsn, "--no-owner", "--no-privileges"], capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode().strip())
    with gzip.open(out, "wb") as f:
        f.write(p.stdout)
    old = sorted(out_dir.glob("interpreter_data_*.sql.gz"))[:-KEEP]
    for o in old:
        o.unlink()
    return out, len(p.stdout)


def mirror_recordings(dest):
    out = ROOT / "nano"
    out.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["rsync", "-a", "--delete", "--exclude", ".bench/", "--stats", dest.rstrip("/") + "/", str(out) + "/"],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    sent = [l for l in p.stdout.splitlines() if l.startswith(("Number of regular files transferred", "Number of files transferred"))]
    return out, sent[0].split(":")[1].strip() if sent else "?"


def install(a):
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plistlib.dump({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve())],
        "StartCalendarInterval": {"Hour": a.hour, "Minute": 0},
        "EnvironmentVariables": {
            "PATH": f"{LIBPQ}:/usr/local/bin:/usr/bin:/bin",
            "PG_DSN": a.dsn, "NANO_SSH": a.ssh, "NANO_DEST": a.dest,
        },
        "StandardOutPath": str(ROOT / "launchd.out"),
        "StandardErrorPath": str(ROOT / "launchd.err"),
    }, open(PLIST, "wb"))
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"installed {PLIST}: runs daily at {a.hour:02d}:00 (Mac must be awake; skipped if the Nano is off)")


def uninstall():
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
    print(f"removed {PLIST}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--hour", type=int, default=21, help="daily run hour for --install (default 21)")
    ap.add_argument("--dsn", default=os.environ.get("PG_DSN"))
    ap.add_argument("--ssh", default=os.environ.get("NANO_SSH"))
    ap.add_argument("--dest", default=os.environ.get("NANO_DEST"))
    a = ap.parse_args()
    if a.uninstall:
        return uninstall()
    for name in ("dsn", "ssh", "dest"):
        if not getattr(a, name):
            sys.exit(f"set --{name} or {'PG_DSN' if name == 'dsn' else 'NANO_' + name.upper()}")
    if a.install:
        return install(a)

    if LIBPQ not in os.environ.get("PATH", "") and not shutil.which("pg_dump"):
        os.environ["PATH"] = LIBPQ + ":" + os.environ.get("PATH", "")
    if not nano_up(a.ssh):
        log("skipped: Nano not reachable")
        return
    try:
        out, n = dump_db(a.dsn)
        log(f"db → {out.name} ({n // 1024} KB uncompressed)")
        out, n = mirror_recordings(a.dest)
        log(f"recordings → {out} ({n} files updated)")
    except Exception as ex:
        log(f"FAILED: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
