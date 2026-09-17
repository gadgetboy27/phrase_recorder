"""Runs ON THE NANO (imported by panel_api.py). Volunteer takes recorded on the touch panel.

A take becomes a normal batch under ~/phrase-recordings/<batch_id>/ — same filename,
manifest.json and audio_samples row as a phone batch (docs/data-contract.md), built with
push.py's build_insert so there is one definition of that INSERT. After the row exists,
whisper-cli drafts the transcript into golden_set as 'draft' (created_by whisper:<model>),
exactly as tools/draft_transcripts.py does for pushed batches. Nothing is ever approved here.

One batch per (speaker, language) while the API runs: one speaker, one language, one session.
"""
import hashlib
import json
import random
import string
import subprocess
import time
from pathlib import Path

import push  # tools/push.py, deployed alongside: build_insert, psql, sql_str

RECORDINGS = Path.home() / "phrase-recordings"
DSN = "postgresql://interpreter_app@localhost:5432/interpreter_data"   # password from ~/.pgpass
APP_VERSION = "panel-1.0"
CONSENT_TEXT_VERSION = 1          # the statement the panel shows == public/index.html's CONSENT_TEXT
SAMPLE_RATE = 48000

_batches = {}                     # (speaker_id, lang) -> batch dir


def batch_dir(speaker_id, lang):
    key = (speaker_id, lang)
    if key not in _batches:
        rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        _batches[key] = RECORDINGS / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{speaker_id}_{lang}_{rand}"
    return _batches[key]


def _manifest(d, lang, speaker):
    mf = d / "manifest.json"
    if mf.exists():
        return json.loads(mf.read_text())
    return {
        "manifest_version": 1, "app_version": APP_VERSION, "batch_id": d.name,
        "exported_at": None, "language": lang,
        "speaker": {"id": speaker["id"], "type": speaker["type"],
                    "consent_confirmed": True, "consent_text_version": CONSENT_TEXT_VERSION},
        "device": {"user_agent": "phrase-panel/ESP32-S3-Touch-LCD-7 + Nano USB mic", "sample_rate": SAMPLE_RATE},
        "recordings": [],
    }


def file_take(tmp_wav, started_at_ms, lang, speaker, phrase, read_text, translation_source):
    """Move a finished take into its batch, extend the manifest, insert the audio_samples row.
    Returns (batch_id, recording dict)."""
    d = batch_dir(speaker["id"], lang)
    d.mkdir(parents=True, exist_ok=True)
    m = _manifest(d, lang, speaker)
    take = 1 + sum(1 for r in m["recordings"] if r["phrase_key"] == phrase["key"])
    name = f"{phrase['key']}v{phrase['version']}_{lang}_{phrase['category']}_asr_training_{speaker['id']}_t{take}_{started_at_ms}.wav"
    wav = d / name
    tmp_wav.rename(wav)
    data = wav.read_bytes()
    rec = {
        "file": name, "phrase_key": phrase["key"], "phrase_version": phrase["version"],
        "phrase_text": phrase["text"], "unmatched": False, "category": phrase["category"],
        "purpose": "asr_training", "take": take,
        "read_text": read_text, "translation_source": translation_source,
        "sample_rate": SAMPLE_RATE, "duration_ms": int((len(data) - 44) / (SAMPLE_RATE * 2) * 1000),
        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started_at_ms / 1000)) + f".{started_at_ms % 1000:03d}Z",
    }
    m["recordings"].append(rec)
    m["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    (d / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False))
    one = dict(m, recordings=[rec])                       # insert just this take (sha256 makes it idempotent)
    sql, _, _ = push.build_insert(one, d.name, None)
    push.psql(DSN, sql)
    return d.name, rec


def draft_transcript(batch_id, rec, lang, phrase, text, model):
    """File whisper's text as a draft golden_set row linked to this take (non-English only)."""
    if lang == "en" or not text:
        return
    q = push.sql_str
    push.psql(DSN, f"""
        BEGIN;
        WITH g AS (
          INSERT INTO golden_set (language_pair, source_text, approved_translation, category, phrase_key, phrase_version, status, created_by)
          VALUES ({q('en-' + lang)}, {q(phrase['text'])}, {q(text)}, {q(phrase['category'])}, {q(phrase['key'])}, {phrase['version']}, 'draft', {q('whisper:' + model)})
          ON CONFLICT (phrase_key, phrase_version, language_pair, approved_translation) DO UPDATE SET status = golden_set.status
          RETURNING id)
        UPDATE audio_samples s SET linked_golden_set_id = coalesce(s.linked_golden_set_id, g.id)
        FROM g WHERE s.file_ref = {q(batch_id + '/' + rec['file'])};
        COMMIT;""")


def db_ok():
    try:
        return push.psql(DSN, "SELECT 1").strip() == "1"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
