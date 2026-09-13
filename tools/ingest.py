#!/usr/bin/env python3
"""Ingest recorder batches on the Mac: validate, stage, report.

    tools/ingest.py ~/Downloads/batch_*.zip
    tools/ingest.py ~/Downloads            # every batch_*.zip in a folder
    tools/ingest.py --derive-16k batch.zip # also write 16 kHz copies (needs ffmpeg)

Reads each zip's manifest.json (docs/data-contract.md §5), checks every WAV
against it (hash, size, header, duration) and against the phrase list
(public/phrases.json), then files the batch under ~/phrase-recordings:

    staged/<batch_id>/    manifest.json + WAVs that passed + ingest-report.json
    rejected/<batch_id>/  anything that failed, with the reason in the report

Nothing here talks to the Nano — that's tools/push.py. Stdlib only.
"""
import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = Path.home() / "phrase-recordings"
DEFAULT_PHRASES = REPO / "public" / "phrases.json"

PURPOSES = {"asr_training", "asr_test", "tts_pronunciation"}
SPEAKER_TYPES = {"native", "fluent", "learner", "staff"}
TRANSLATION_SOURCES = {"canonical", "approved", "typed"}
SPEAKER_ID_RE = re.compile(r"^[a-z0-9]{2,12}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.wav$")
DURATION_TOLERANCE_MS = 50


class Problem(Exception):
    pass


def load_phrase_list(path):
    data = json.loads(Path(path).read_text())
    return {
        "languages": {l["code"] for l in data["languages"]},
        "categories": {c["slug"] for c in data["categories"]},
        "phrases": {(p["key"], p["version"]): p for p in data["phrases"]},
    }


def check_manifest(m, lists):
    """Batch-level validation. Raises Problem on anything that rejects the whole batch."""
    if m.get("manifest_version") != 1:
        raise Problem(f"unsupported manifest_version {m.get('manifest_version')!r}")
    for k in ("batch_id", "exported_at", "language", "speaker", "device", "recordings"):
        if k not in m:
            raise Problem(f"manifest missing {k!r}")
    if not re.match(r"^[0-9]{8}T[0-9]{6}Z_[a-z0-9]{2,12}_[a-z]{2,3}_[a-z0-9]{4,8}$", m["batch_id"]):
        raise Problem(f"batch_id has unexpected shape: {m['batch_id']!r}")
    if m["language"] not in lists["languages"]:
        raise Problem(f"language {m['language']!r} not in phrases.json")
    sp = m["speaker"]
    if not SPEAKER_ID_RE.match(sp.get("id", "")):
        raise Problem(f"speaker.id {sp.get('id')!r} invalid")
    if sp.get("type") not in SPEAKER_TYPES:
        raise Problem(f"speaker.type {sp.get('type')!r} invalid")
    if sp.get("consent_confirmed") is not True:
        raise Problem("speaker.consent_confirmed is not true — cannot ingest without consent")
    if not isinstance(m["recordings"], list) or not m["recordings"]:
        raise Problem("manifest has no recordings")


def m_lang(lists):
    return lists.get("_batch_language")


def check_recording(r, data, lists):
    """Per-recording validation. Returns (errors, warnings)."""
    errors, warnings = [], []
    f = r.get("file", "")
    if not FILE_RE.match(f):
        errors.append(f"bad filename {f!r}")
    if r.get("purpose") not in PURPOSES:
        errors.append(f"purpose {r.get('purpose')!r} invalid")
    if r.get("category") not in lists["categories"]:
        errors.append(f"category {r.get('category')!r} not in phrases.json")
    if not isinstance(r.get("take"), int) or r["take"] < 1:
        errors.append("take must be a positive integer")
    if not r.get("phrase_text", "").strip():
        errors.append("phrase_text empty")
    # v2.1: what was read, and where that text came from (both optional, must agree).
    rt, src = r.get("read_text"), r.get("translation_source")
    if src is not None and src not in TRANSLATION_SOURCES:
        errors.append(f"translation_source {src!r} invalid")
    if (rt is None) != (src is None):
        errors.append("read_text and translation_source must both be set or both be null")
    if rt is not None and not str(rt).strip():
        errors.append("read_text is blank")
    if rt is None and m_lang(lists) != "en" and not r.get("unmatched"):
        warnings.append("no translation text — transcript can be added on the Nano later")

    if r.get("unmatched"):
        if r.get("phrase_key") is not None:
            errors.append("unmatched recording must not carry a phrase_key")
    else:
        key = (r.get("phrase_key"), r.get("phrase_version"))
        known = lists["phrases"].get(key)
        if known is None:
            warnings.append(f"phrase {key[0]}v{key[1]} not in phrases.json — will be filed as unmatched on the Nano")
        elif known["text"] != r.get("phrase_text"):
            warnings.append(f"phrase_text differs from phrases.json for {key[0]}v{key[1]}")
        elif known["category"] != r.get("category"):
            warnings.append(f"category {r.get('category')} differs from phrases.json ({known['category']}) for {key[0]}v{key[1]}")

    # The bytes themselves.
    if data is None:
        errors.append("file missing from zip")
        return errors, warnings
    if len(data) != r.get("bytes"):
        errors.append(f"size {len(data)} != manifest bytes {r.get('bytes')}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != r.get("sha256"):
        errors.append("sha256 mismatch")
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if ch != 1:
            errors.append(f"{ch} channels (expected mono)")
        if sw != 2:
            errors.append(f"{sw * 8}-bit (expected 16)")
        if fr != r.get("sample_rate"):
            errors.append(f"header sample rate {fr} != manifest {r.get('sample_rate')}")
        dur = round(n / fr * 1000)
        if abs(dur - int(r.get("duration_ms", 0))) > DURATION_TOLERANCE_MS:
            errors.append(f"duration {dur}ms != manifest {r.get('duration_ms')}ms")
        if dur < 300:
            errors.append(f"too short ({dur}ms)")
    except wave.Error as e:
        errors.append(f"not a valid WAV: {e}")
    return errors, warnings


def derive_16k(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(dst)],
        check=True,
    )


def ingest_zip(zpath, root, lists, derive, force):
    print(f"\n== {zpath}")
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            return reject_whole(root, zpath, "no manifest.json in zip", None)
        try:
            m = json.loads(z.read("manifest.json"))
            check_manifest(m, lists)
        except (json.JSONDecodeError, Problem, KeyError, TypeError) as e:
            return reject_whole(root, zpath, f"manifest invalid: {e}", None)

        batch_id = m["batch_id"]
        lists["_batch_language"] = m["language"]
        staged = root / "staged" / batch_id
        pushed = root / "pushed" / batch_id
        if pushed.exists():
            print(f"   already pushed to the Nano ({pushed}) — skipping")
            return None
        if staged.exists() and not force:
            print(f"   already staged ({staged}) — skipping (use --force to redo)")
            return None
        if staged.exists():
            shutil.rmtree(staged)

        accepted, rejected = [], []
        for r in m["recordings"]:
            data = z.read(r["file"]) if r.get("file") in names else None
            errors, warnings = check_recording(r, data, lists)
            entry = {"file": r.get("file"), "warnings": warnings}
            if errors:
                entry["errors"] = errors
                rejected.append((r, data, entry))
                print(f"   REJECT {r.get('file')}: {'; '.join(errors)}")
            else:
                accepted.append((r, data, entry))
                flag = "  ! " + "; ".join(warnings) if warnings else ""
                print(f"   ok     {r['file']}{flag}")

        # Extra files in the zip that the manifest doesn't mention.
        extra = sorted(names - {"manifest.json"} - {r.get("file") for r in m["recordings"]})
        if extra:
            print(f"   note: {len(extra)} file(s) in zip not listed in manifest, ignored: {extra[:5]}")

        report = {
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "source_zip": str(Path(zpath).resolve()),
            "batch_id": batch_id,
            "accepted": [e for _, _, e in accepted],
            "rejected": [e for _, _, e in rejected],
            "extra_files_ignored": extra,
        }

        if accepted:
            staged.mkdir(parents=True)
            for r, data, _ in accepted:
                (staged / r["file"]).write_bytes(data)
                if derive:
                    derive_16k(staged / r["file"], staged / "derived" / "16k" / r["file"])
            staged_manifest = dict(m, recordings=[r for r, _, _ in accepted])
            (staged / "manifest.json").write_text(json.dumps(staged_manifest, indent=2, ensure_ascii=False))
            (staged / "ingest-report.json").write_text(json.dumps(report, indent=2))
        if rejected:
            rdir = root / "rejected" / batch_id
            rdir.mkdir(parents=True, exist_ok=True)
            for r, data, _ in rejected:
                if data is not None:
                    (rdir / r["file"]).write_bytes(data)
            (rdir / "ingest-report.json").write_text(json.dumps(report, indent=2))

        n_warn = sum(1 for _, _, e in accepted if e["warnings"])
        print(f"   → {len(accepted)} staged, {len(rejected)} rejected, {n_warn} with warnings"
              + (f"  ({staged})" if accepted else ""))
        return report


def reject_whole(root, zpath, reason, batch_id):
    name = batch_id or Path(zpath).stem
    rdir = root / "rejected" / name
    rdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(zpath, rdir / "source.zip")
    report = {"ingested_at": datetime.now(timezone.utc).isoformat(), "source_zip": str(Path(zpath).resolve()), "rejected_batch": reason}
    (rdir / "ingest-report.json").write_text(json.dumps(report, indent=2))
    print(f"   REJECTED BATCH: {reason}  ({rdir})")
    return report


def expand(paths):
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            out += sorted(p.glob("batch_*.zip"))
        elif p.suffix.lower() == ".zip":
            out.append(p)
        else:
            print(f"skipping {p}: not a zip or folder", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="batch_*.zip files, or folders containing them")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help=f"staging root (default {DEFAULT_ROOT})")
    ap.add_argument("--phrases", default=str(DEFAULT_PHRASES), help="phrases.json to validate against")
    ap.add_argument("--derive-16k", action="store_true", help="also write 16 kHz copies under derived/16k (needs ffmpeg)")
    ap.add_argument("--force", action="store_true", help="re-stage a batch that is already staged")
    a = ap.parse_args()

    if a.derive_16k and not shutil.which("ffmpeg"):
        sys.exit("--derive-16k needs ffmpeg on PATH (brew install ffmpeg)")
    root = Path(a.root).expanduser()
    for d in ("incoming", "staged", "rejected", "pushed"):
        (root / d).mkdir(parents=True, exist_ok=True)
    lists = load_phrase_list(a.phrases)

    zips = expand(a.paths)
    if not zips:
        sys.exit("no batch_*.zip files found")
    reports = [ingest_zip(z, root, lists, a.derive_16k, a.force) for z in zips]
    done = [r for r in reports if r]
    bad = [r for r in done if r.get("rejected_batch") or r.get("rejected")]
    print(f"\n{len(done)} batch(es) processed, {len(bad)} with rejections. Next: tools/push.py --all")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
