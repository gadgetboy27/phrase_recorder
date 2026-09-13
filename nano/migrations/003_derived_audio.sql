-- 003: where the trimmed 16 kHz training copy of each recording lives.
--
-- The original WAV (file_ref) is never touched — data-contract.md §1. The
-- derived copy is what Whisper and fine-tuning actually consume: speech
-- boundaries found by Silero VAD on the Nano, padded, resampled to 16 kHz.
-- Written by tools/bench.py (via nano/asr_worker.py). Idempotent.

BEGIN;

ALTER TABLE audio_samples
  ADD COLUMN IF NOT EXISTS derived_ref     text,   -- '<batch_id>/derived/16k-trim/<file>' on the Nano
  ADD COLUMN IF NOT EXISTS speech_start_ms int,    -- VAD boundaries in the ORIGINAL file
  ADD COLUMN IF NOT EXISTS speech_end_ms   int;

COMMIT;
