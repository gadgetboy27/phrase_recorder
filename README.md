# Phrase Recorder

Mobile-friendly tool for collecting phrase audio (native/fluent speaker
recordings) for the local medical interpreter project. Deployed as a static
site via Cloudflare Pages.

## Full context

See [`docs/project-brief.md`](./docs/project-brief.md) for the complete
design — architecture, model stack, training methodology, database schema,
consent rules, iOS deployment plan, and the Jetson Nano Postgres setup.
Read that before making structural changes here; this README is only an
orientation pointer, not a substitute for it.

## Current state

- `index.html` — the recorder itself. Records locally in-browser (no
  backend yet), builds a WAV with a filename encoding phrase ID + version,
  language, category, and purpose, matching the schema on the Nano's
  Postgres database (see brief §16 for the schema, §12–13 for background
  on why phrase IDs are versioned).
- Deployed via Cloudflare Pages, connected to this repo — push to `main`
  and it deploys automatically.
- No API yet. The phrase list is hardcoded in `index.html`; recordings are
  downloaded manually and transferred to the Nano via `scp`.

## What's next (not yet built)

1. A small API on the Nano (FastAPI) — `GET /phrases`, `POST /phrases`,
   `POST /recordings` — reading and writing the existing Postgres schema.
2. A Cloudflare Tunnel exposing that API to this frontend without opening
   the Nano's network to the internet.
3. Swap `index.html`'s hardcoded phrase list for a live fetch from the
   API, and add a real upload flow in place of the download button.

Full step-by-step breakdown of this sequence is in the project brief.

## Don't rebuild what's already decided

A few things were deliberately settled during design — check the brief
before re-deciding them:
- Phrase IDs are language-neutral and versioned (`p005v1`), not per-language.
- Postgres is the source of truth; any exported file (e.g. `manifest.json`)
  is generated from it, never the other way round.
- Nothing patient-adjacent goes through a managed cloud service — the
  database stays self-hosted, on your own hardware, always.
