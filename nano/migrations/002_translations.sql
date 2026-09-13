-- 002: link recordings to translation text (golden_set) and capture what was read.
--
-- The recorder now shows the approved target-language text when one exists,
-- or lets the volunteer type their translation. Either way the exact text
-- read is stored on the recording, and typed translations become draft
-- golden_set rows for review. Idempotent.

BEGIN;

-- What the speaker actually read (target-language text, or the English
-- canonical text for 'en' recordings). NULL when a non-English volunteer
-- recorded without typing a translation — never silently the English.
ALTER TABLE audio_samples
  ADD COLUMN IF NOT EXISTS read_text          text,
  ADD COLUMN IF NOT EXISTS translation_source text
    CHECK (translation_source IN ('canonical','approved','typed'));

-- golden_set: one approved translation per phrase per language pair,
-- drafts allowed alongside, exact duplicates collapsed.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'golden_set_status_chk') THEN
    ALTER TABLE golden_set
      ADD CONSTRAINT golden_set_status_chk
      CHECK (status IN ('draft','approved','rejected','superseded'));
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS golden_set_one_approved_uq
  ON golden_set (phrase_key, phrase_version, language_pair)
  WHERE status = 'approved';

CREATE UNIQUE INDEX IF NOT EXISTS golden_set_text_uq
  ON golden_set (phrase_key, phrase_version, language_pair, approved_translation);

CREATE INDEX IF NOT EXISTS golden_set_phrase_idx ON golden_set (phrase_key, phrase_version);

COMMIT;
