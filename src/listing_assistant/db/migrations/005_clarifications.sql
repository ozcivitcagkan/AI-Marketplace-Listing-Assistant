-- Migration 005: questions asked to the seller about missing required fields.

CREATE TABLE clarifications (
    id              TEXT PRIMARY KEY,
    listing_id      TEXT NOT NULL REFERENCES listings (id),
    field_key       TEXT NOT NULL,
    question        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'answered', 'declined')),
    answer_fact_id  TEXT REFERENCES listing_facts (id),
    created_at      TEXT NOT NULL,
    CHECK ((status = 'answered') = (answer_fact_id IS NOT NULL))
);
CREATE INDEX idx_clarifications_listing ON clarifications (listing_id);

-- A question is answered once: only an open question may change, and only its outcome.
CREATE TRIGGER clarifications_close_once BEFORE UPDATE ON clarifications
WHEN OLD.status != 'open'
  OR NEW.id IS NOT OLD.id
  OR NEW.listing_id IS NOT OLD.listing_id
  OR NEW.field_key IS NOT OLD.field_key
  OR NEW.question IS NOT OLD.question
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only an open clarification can be answered or declined'); END;
CREATE TRIGGER clarifications_no_delete BEFORE DELETE ON clarifications
BEGIN SELECT RAISE(ABORT, 'clarifications are never deleted'); END;

-- The answer must be a fact of the same listing.
CREATE TRIGGER clarifications_answer_same_listing BEFORE UPDATE OF answer_fact_id ON clarifications
WHEN NEW.answer_fact_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM listing_facts WHERE id = NEW.answer_fact_id AND listing_id = NEW.listing_id
)
BEGIN SELECT RAISE(ABORT, 'answer fact must belong to the same listing'); END;

PRAGMA user_version = 5;
