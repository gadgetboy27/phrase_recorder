# Data contract — recordings from phone to Nano

This is the one document every part of the pipeline agrees on. If you
change anything here, change it in all four places listed under
[Where it's enforced](#where-its-enforced) in the same commit.

```
volunteer's phone ──► batch.zip ──► Mac (tools/ingest.py) ──► Nano (tools/push.py)
  public/index.html     WAVs +       validate, stage, QC       rsync + INSERT
                        manifest.json                          audio_samples
```

## 1. Audio format

- **WAV, PCM 16-bit, mono**, at the device's native sample rate (44 100 or
  48 000 Hz — whatever the browser's `AudioContext` gives). The browser
  never resamples; it just writes what the mic delivered.
- Lossless end to end. No MP4/AAC/Opus anywhere in the pipeline — ASR and
  TTS training should never see compression artefacts. If archive size
  ever matters, the Mac ingest step can add a FLAC copy; the original WAV
  is always kept.
- Resampling (e.g. to 16 kHz for Whisper) is a *derived* artefact produced
  on the Mac/Nano, never done in the browser.

## 2. Phrase identity

- A phrase is identified by `phrase_key` + `version`, e.g. `p005` v`1`,
  written `p005v1`. The key is **language-neutral** — the same phrase
  recorded in Spanish and Hindi shares one key.
- The list of phrases the recorder shows comes from `public/phrases.json`.
  The Nano's `phrases` table is seeded with the same rows
  (`nano/migrations/001_*.sql`). Until the API exists these are kept in
  sync by hand; `tools/ingest.py --phrases public/phrases.json` warns when
  a manifest references a key that isn't in the file.
- A volunteer can add a phrase that isn't in the list. It gets no key —
  it's exported as `unmatched: true` with the typed text, and the filename
  uses `UNASSIGNED_<slug>`. On import it lands in `audio_samples` with
  `phrase_key = NULL` and `unmatched_phrase_text` set. **Nothing ever
  guesses a key.**

## 3. Speaker identity and consent

- `speaker_id`: 2–12 chars, lower-case letters/digits, chosen by the
  volunteer or assigned by whoever runs the session. Initials or a
  pseudonym — **never a full name**; it ends up in filenames.
- `speaker_type`: `native` | `fluent` | `learner` | `staff`.
- `consent_confirmed` must be `true` for a batch to be exported. The
  consent statement shown in the recorder is versioned
  (`consent_text_version`) so we know what someone agreed to.

## 4. Filename

Human-readable fallback only — the manifest is authoritative.

```
{phraseId}_{lang}_{category}_{purpose}_{speaker}_t{take}_{recordedAtMs}.wav

p005v1_es_obstetric_emergency_asr_training_mg_t2_1757751234567.wav
UNASSIGNED_where_does_it_hurt_es_symptoms_asr_training_mg_t1_1757751299001.wav
```

`{phraseId}` is `pNNNvN`, or `UNASSIGNED_<first 40 chars of slug>`.

## 5. `manifest.json` (v1)

One per batch. A batch is one speaker, one language, one export.

```json
{
  "manifest_version": 1,
  "app_version": "2.0.0",
  "batch_id": "20260913T101500Z_mg_es_a1b2c3",
  "exported_at": "2026-09-13T10:15:00.000Z",
  "language": "es",
  "speaker": {
    "id": "mg",
    "type": "native",
    "consent_confirmed": true,
    "consent_text_version": 1
  },
  "device": {
    "user_agent": "Mozilla/5.0 (iPhone; ...)",
    "sample_rate": 48000
  },
  "recordings": [
    {
      "file": "p005v1_es_obstetric_emergency_asr_training_mg_t2_1757751234567.wav",
      "phrase_key": "p005",
      "phrase_version": 1,
      "phrase_text": "We need to perform an emergency caesarean section right now to protect your baby.",
      "unmatched": false,
      "category": "obstetric_emergency",
      "purpose": "asr_training",
      "take": 2,
      "sample_rate": 48000,
      "duration_ms": 4120,
      "bytes": 395564,
      "sha256": "9f2c…",
      "recorded_at": "2026-09-13T10:07:14.567Z"
    }
  ]
}
```

Rules:
- `sha256` is over the full WAV file (header included). It is the
  dedupe key on the Nano (`audio_samples.sha256` is unique), so pushing
  the same batch twice is harmless.
- `purpose` ∈ `asr_training` | `asr_test` | `tts_pronunciation` — the
  same CHECK as `audio_samples.purpose`.
- `category` ∈ `phrase_categories.value`.
- `language` ∈ `languages.code`.
- For `unmatched: true` recordings, `phrase_key`/`phrase_version` are
  `null` and `phrase_text` is the text the volunteer typed.

## 6. ZIP layout

```
batch_20260913T101500Z_mg_es_a1b2c3.zip
├── manifest.json
├── p005v1_es_obstetric_emergency_asr_training_mg_t2_1757751234567.wav
└── …
```

Stored (uncompressed) entries — WAV barely compresses and it keeps the
browser-side zip writer dependency-free.

## 7. Mac staging layout

```
~/phrase-recordings/
├── incoming/     drop zips / loose WAVs here (or point ingest.py at ~/Downloads)
├── staged/<batch_id>/   validated batches: manifest.json + WAVs + ingest-report.json
├── rejected/<batch_id>/ anything that failed validation, with the reason
└── pushed/<batch_id>/   moved here by push.py after the Nano accepted it
```

## 8. Nano

- Files: `/srv/phrase-recordings/<batch_id>/…` (rsync'd verbatim).
- Rows: one `audio_samples` row per recording, `file_ref` =
  `<batch_id>/<file>`. Columns map 1:1 to the manifest fields above.
- `phrases` table is the source of truth for keys. If a manifest claims a
  key that doesn't exist there, the row is inserted with
  `phrase_key = NULL`, `unmatched_phrase_text = phrase_text` and
  `notes = 'claimed p0XXvN not in phrases'` — and push.py prints it.

## Where it's enforced

| Piece | File |
|---|---|
| Recorder (produces zip + manifest) | `public/index.html` |
| Phrase list | `public/phrases.json` |
| Mac ingest (validates, stages) | `tools/ingest.py` |
| Nano push (rsync + INSERT) | `tools/push.py` |
| Schema | `nano/migrations/001_phrases_and_audio_metadata.sql` |
