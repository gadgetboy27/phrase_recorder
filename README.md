# Phrase Recorder

Mobile-friendly tool for collecting phrase audio (native/fluent speaker
recordings) for the local medical interpreter project, plus the tools that
carry those recordings from a volunteer's phone to the Jetson Nano.

## Full context

See [`docs/project-brief.md`](./docs/project-brief.md) for the complete
design — architecture, model stack, training methodology, database schema,
consent rules, iOS deployment plan, and the Jetson Nano Postgres setup.
Read that before making structural changes here; this README is only an
orientation pointer, not a substitute for it.

[`docs/data-contract.md`](./docs/data-contract.md) is the contract every
piece below conforms to (audio format, phrase IDs, filename, manifest, zip
layout, staging layout, Nano rows). Change it there first.

## The pipeline

```
volunteer's phone ──► batch.zip ──► Mac ──────────────────► Nano
  public/index.html    AirDrop /    tools/ingest.py         tools/push.py
  record, listen,      share sheet  validate + stage        rsync files
  accept, export                    (your QC gate)          INSERT audio_samples
```

| Piece | Path | What it does |
|---|---|---|
| Recorder | `public/index.html` | Static page on Cloudflare Pages. Volunteer picks language, speaker ID, consent; steps through a category's phrases; each accepted take is a 16-bit mono WAV at the device's native rate. Takes persist in IndexedDB. **Export batch** builds one zip (WAVs + `manifest.json`) and opens the share sheet (AirDrop on iPhone) or downloads it. |
| Phrase list | `public/phrases.json` | Languages, categories, and phrases (`key` + `version`, language-neutral). The recorder fetches this. Generated from the Nano's tables by `tools/export_phrases.py` — never edit by hand. |
| Ingest | `tools/ingest.py` | On the Mac. Verifies every WAV against the manifest (SHA-256, size, header, duration) and the phrase list, stages good batches under `~/phrase-recordings/staged/`, files failures under `rejected/` with a report. |
| Phrase sync | `tools/export_phrases.py` | Regenerates `public/phrases.json` from the Nano's `phrases`, `phrase_categories`, and `languages` tables. `--check` reports whether the file is stale without writing. |
| Push | `tools/push.py` | rsyncs a staged batch to the Nano and inserts `audio_samples` rows. Dedupes on SHA-256 so re-pushing is harmless. Unknown phrase keys are filed as unmatched and printed, never guessed. Typed translations become draft `golden_set` rows linked to their recordings. |
| Add phrases | `tools/add_phrase.py` | `--category intake "…"` adds the next key; `--revise p003 "…"` adds a new version. Refreshes `phrases.json` for you. |
| Draft transcripts | `tools/draft_transcripts.py` | `--lang fa`: Whisper on the Nano transcribes every recording whose phrase has no approved text in that language and files the result as a `draft` golden_set row (`created_by whisper:<model>`) for a reader of the language to approve or reject. Never auto-approves. |
| Review translations | `tools/translations.py` | `list` drafts, `play` a draft's recording on the Nano, `approve` / `reject`, or `set p003 mi "…"` to enter one directly. |
| Bench | `tools/bench.py` | The bake-off number. For every approved translation: trims each linked recording to its speech (Silero VAD) and resamples to 16 kHz on the Nano — the original WAV is untouched — transcribes it with whisper-cli, asks the Nano's llama-server to translate the English, and scores both against the approved text (CER/WER, with and without macrons). Report under `~/phrase-recordings/bench/`; derived file + speech boundaries recorded on `audio_samples`. |
| Nano worker | `nano/asr_worker.py` | The half of bench that runs on the Nano (copied over by `bench.py`). Uses `~/whisper.cpp` (CUDA build, `ggml-large-v3-turbo-q5_0.bin`, `ggml-silero-v6.2.0.bin`), sox, and the llama-server on `127.0.0.1:8080`. |
| Touch panel | `nano/panel.yaml` + `nano/panel_api.py` | Firmware (ESPHome + LVGL) for the Waveshare ESP32-S3-Touch-LCD-7 and the Nano-side API it talks to on `:8765`. The panel builds its screen from `GET /phrases` (categories, languages, phrases with translations) — nothing is hard-coded. Tap a phrase → the Nano says it in the selected language: a native/fluent speaker's pushed recording if there is one, else Piper on the approved translation, else a learner's recording, else the English. Record/Stop → whisper-cli transcript in that language (+ English via llama-server when it's up). Clinician/heard bubbles use the brief's sage/steel palette. `tools/nano.sh panel` deploys the API (`@reboot` cron keeps it up). Flash the panel once over USB: `pip install esphome`, copy `nano/secrets.yaml.example` → `nano/secrets.yaml`, `esphome run nano/panel.yaml`; later updates go over Wi-Fi. |
| Backup | `tools/backup.py` | `pg_dump` of the Nano's database (the golden set) plus an rsync mirror of its recordings, into `~/phrase-recordings/backups/`. `--install` schedules it daily at 21:00 via launchd; skips quietly when the Nano is off. Last 30 dumps kept. |
| Schema | `nano/migrations/` + `nano/apply.sh` | Idempotent migrations on top of the brief's §16 schema. `001` adds the `phrases` table and the recording metadata columns; `002` translations; `003` derived-audio columns. |

## Setup

**Nano (once):** after the §16 setup, hand the tables to the app user (they
are created as `postgres`) and make the recordings directory:

```bash
sudo -u postgres psql -d interpreter_data -c 'ALTER TABLE languages OWNER TO interpreter_app; ALTER TABLE phrase_categories OWNER TO interpreter_app; ALTER TABLE golden_set OWNER TO interpreter_app; ALTER TABLE corrections OWNER TO interpreter_app; ALTER TABLE training_runs OWNER TO interpreter_app; ALTER TABLE dataset_snapshots OWNER TO interpreter_app; ALTER TABLE prompt_versions OWNER TO interpreter_app; ALTER TABLE audio_samples OWNER TO interpreter_app;'
mkdir -p ~/phrase-recordings                 # where pushed batches land
amixer -c 0 sset Speaker 100% && sudo alsactl store   # USB speaker volume, persisted
```

Migrations then run from the Mac as `interpreter_app`: `nano/apply.sh`
(idempotent — rerun after pulling a new migration).

**Mac (once):**

```bash
brew install libpq && brew link --force libpq     # psql
export NANO_DEST=gadgetboy@192.168.68.111:/home/gadgetboy/phrase-recordings
export NANO_SSH=gadgetboy@192.168.68.111
export PG_DSN="postgresql://interpreter_app@192.168.68.111:5432/interpreter_data"
# password goes in ~/.pgpass (chmod 600), never in the DSN or the repo:
#   192.168.68.111:5432:interpreter_data:interpreter_app:PASSWORD
```

## Day-to-day

`tools/nano.sh` is the menu for all of this — it sets the env vars itself,
shows whether the Nano is up, and offers status / start llama-server /
ingest / push / bench / translations / backup / shutdown. Or step by step:

1. Volunteer opens https://phrase-recorder.pages.dev on their phone, records,
   taps **Export batch**, AirDrops the zip to the Mac.
2. `tools/ingest.py ~/Downloads` — validates everything in the folder. Listen
   to anything in `~/phrase-recordings/staged/<batch>/` you want to check;
   delete a WAV from the staged manifest if it shouldn't go up.
3. `tools/push.py --all` — files to the Nano, rows into Postgres, batch moved
   to `pushed/`.
4. Tell the volunteer it arrived so they can clear the batch on their phone.

`tools/push.py --all --dry-run` shows the rsync command and SQL without doing
anything.

**Backups:** run nightly by launchd once `tools/backup.py --install` has been
done on the Mac (it has). `tools/backup.py` runs one now; check
`~/phrase-recordings/backups/backup.log`. Restore with
`gunzip -c backups/db/<file>.sql.gz | psql interpreter_data`.

**Scoring the models:** `tools/bench.py --lang mi` after any push or approval.
Every recording is trimmed + resampled into `<batch>/derived/16k-trim/` on
the Nano (roughly a quarter of the original size); that copy is what
Whisper — and later fine-tuning — reads. `--model` swaps the Whisper model,
`--no-translate` skips the LLM, `--force` re-trims.

**Adding phrases:** `tools/add_phrase.py --category intake "What is your
date of birth?"` then commit and push `public/phrases.json`.

**Recordings without text:** `tools/draft_transcripts.py --lang fa` lets
Whisper propose the transcript; the drafts then go through the same
review as typed ones. Persian/Arabic/Hindi/Mandarin/Vietnamese/Spanish are
strong in Whisper so the drafts are close; Te Reo Māori is not — expect to
correct those by hand.

**Translations:** a non-English volunteer sees the approved translation to
read if one exists; otherwise the English plus an optional box to type
their translation. Typed ones arrive as drafts — `tools/translations.py
list`, listen with `play <id>`, then `approve <id>`, then
`tools/export_phrases.py`, commit, push. From then on every volunteer
reads the same approved text. A blank box never stores the English as the
translation; the recording is kept and the text can be added later.

## Deploy

Cloudflare Pages project `phrase-recorder`, connected to this repo. Push to
`main` → production; any other branch → preview URL. Output dir is `public/`
so `docs/`, `tools/`, `nano/` are never published.

## What's next (not yet built)

The Mac hop exists so the Nano's network never has to be opened up and so
there's a human QC step. When that's no longer the bottleneck:

1. FastAPI on the Nano — `GET /phrases`, `POST /recordings` — accepting
   exactly the manifest + files the zip contains, reusing `ingest.py`'s
   validation.
2. Cloudflare Tunnel (+ Access) in front of it.
3. Recorder swaps `phrases.json` for `GET /phrases` and gains an **Upload**
   button next to **Export**.

## Don't rebuild what's already decided

- Phrase IDs are language-neutral and versioned (`p005v1`), not per-language.
- Postgres on the Nano is the source of truth; `phrases.json` is a copy of it
  until the API exists, never the other way round.
- WAV stays lossless end to end. Derived formats (16 kHz, FLAC) are produced
  on the Mac/Nano, never in the browser.
- Nothing patient-adjacent goes through a managed cloud service — the
  database stays self-hosted, on your own hardware, always.
