-- 001: language-neutral versioned phrases + full recording metadata.
--
-- Incremental against the schema created by docs/project-brief.md §16.
-- Idempotent: safe to re-run. Apply with nano/apply.sh.
--
-- Why: the recorder labels every take with a phrase key + version
-- (p005v1) that is the same across languages, but §16's golden_set only
-- had a serial id and audio_samples only had a link to a golden_set row
-- (which is per language pair). This adds the missing concept so an
-- import can match a recording to a phrase — or flag it as unmatched —
-- rather than guess.

BEGIN;

CREATE TABLE IF NOT EXISTS phrases (
  phrase_key      text NOT NULL,                       -- 'p001'
  version         int  NOT NULL DEFAULT 1,
  category        text NOT NULL REFERENCES phrase_categories(value),
  canonical_text  text NOT NULL,                       -- source wording (English)
  status          text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','superseded','retired')),
  created_at      timestamptz DEFAULT now(),
  PRIMARY KEY (phrase_key, version)
);

-- golden_set rows (per language pair) now point at the phrase they translate.
ALTER TABLE golden_set
  ADD COLUMN IF NOT EXISTS phrase_key     text,
  ADD COLUMN IF NOT EXISTS phrase_version int;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'golden_set_phrase_fk') THEN
    ALTER TABLE golden_set
      ADD CONSTRAINT golden_set_phrase_fk
      FOREIGN KEY (phrase_key, phrase_version) REFERENCES phrases(phrase_key, version);
  END IF;
END $$;

-- audio_samples: everything the recorder's manifest.json carries.
ALTER TABLE audio_samples
  ADD COLUMN IF NOT EXISTS phrase_key            text,
  ADD COLUMN IF NOT EXISTS phrase_version        int,
  ADD COLUMN IF NOT EXISTS unmatched_phrase_text text,   -- set when no phrase row matched
  ADD COLUMN IF NOT EXISTS category              text REFERENCES phrase_categories(value),
  ADD COLUMN IF NOT EXISTS speaker_id            text,   -- initials/pseudonym, never a full name
  ADD COLUMN IF NOT EXISTS take                  int,
  ADD COLUMN IF NOT EXISTS sample_rate           int,
  ADD COLUMN IF NOT EXISTS duration_ms           int,
  ADD COLUMN IF NOT EXISTS sha256                text,
  ADD COLUMN IF NOT EXISTS recorded_at           timestamptz,
  ADD COLUMN IF NOT EXISTS device                text,
  ADD COLUMN IF NOT EXISTS consent_confirmed     boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS batch_id              text,
  ADD COLUMN IF NOT EXISTS notes                 text;

-- Same audio can't be imported twice (also what makes re-running push safe).
CREATE UNIQUE INDEX IF NOT EXISTS audio_samples_sha256_uq ON audio_samples (sha256);
CREATE INDEX IF NOT EXISTS audio_samples_phrase_idx ON audio_samples (phrase_key, phrase_version, language);
CREATE INDEX IF NOT EXISTS audio_samples_batch_idx  ON audio_samples (batch_id);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'audio_samples_phrase_fk') THEN
    ALTER TABLE audio_samples
      ADD CONSTRAINT audio_samples_phrase_fk
      FOREIGN KEY (phrase_key, phrase_version) REFERENCES phrases(phrase_key, version);
  END IF;
  -- A row is either matched to a phrase or carries the unmatched text — never neither.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'audio_samples_phrase_or_text') THEN
    ALTER TABLE audio_samples
      ADD CONSTRAINT audio_samples_phrase_or_text
      CHECK (phrase_key IS NOT NULL OR unmatched_phrase_text IS NOT NULL);
  END IF;
END $$;

-- Seed: the obstetric emergency bank the recorder currently ships with.
-- Keep in sync with public/phrases.json (tools/ingest.py --phrases checks this).
INSERT INTO phrases (phrase_key, version, category, canonical_text) VALUES
  ('p001', 1, 'obstetric_emergency', 'The umbilical cord is compressed. We must act immediately.'),
  ('p002', 1, 'obstetric_emergency', 'Do you understand and consent to this emergency procedure?'),
  ('p003', 1, 'obstetric_emergency', 'Please stay as still as possible for me.'),
  ('p004', 1, 'obstetric_emergency', 'Your baby''s heart rate has dropped. This is an emergency.'),
  ('p005', 1, 'obstetric_emergency', 'We need to perform an emergency caesarean section right now to protect your baby.'),
  ('p006', 1, 'obstetric_emergency', 'We are going to give you an injection to help you and your baby.'),
  ('p007', 1, 'obstetric_emergency', 'Try to breathe slowly and deeply.'),
  ('p008', 1, 'obstetric_emergency', 'The baby is coming now, you need to push.'),
  ('p009', 1, 'obstetric_emergency', 'We need to monitor your baby''s heartbeat continuously.'),
  ('p010', 1, 'obstetric_emergency', 'Is there anyone you would like us to call?')
ON CONFLICT (phrase_key, version) DO NOTHING;

COMMIT;
