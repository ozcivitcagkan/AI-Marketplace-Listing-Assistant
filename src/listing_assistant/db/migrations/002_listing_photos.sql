-- Migration 002: photo metadata and quality scores. Files live on disk, not in the DB.

CREATE TABLE listing_photos (
    id                     TEXT PRIMARY KEY,
    listing_id             TEXT NOT NULL REFERENCES listings (id),
    storage_key            TEXT NOT NULL UNIQUE,
    sha256                 TEXT NOT NULL,
    perceptual_hash        TEXT NOT NULL,
    width                  INTEGER NOT NULL CHECK (width > 0),
    height                 INTEGER NOT NULL CHECK (height > 0),
    blur_score             REAL NOT NULL,
    brightness             REAL NOT NULL,
    quality_warnings_json  TEXT NOT NULL,
    privacy_flags_json     TEXT NOT NULL,
    order_index            INTEGER NOT NULL CHECK (order_index >= 0),
    is_cover               INTEGER NOT NULL CHECK (is_cover IN (0, 1)),
    created_at             TEXT NOT NULL,
    UNIQUE (listing_id, sha256)
);
CREATE INDEX idx_listing_photos_listing ON listing_photos (listing_id);

-- Measurements are facts about the file; only ordering, cover choice and privacy flags move.
CREATE TRIGGER listing_photos_only_presentation_changes BEFORE UPDATE ON listing_photos
WHEN NEW.id IS NOT OLD.id
  OR NEW.listing_id IS NOT OLD.listing_id
  OR NEW.storage_key IS NOT OLD.storage_key
  OR NEW.sha256 IS NOT OLD.sha256
  OR NEW.perceptual_hash IS NOT OLD.perceptual_hash
  OR NEW.width IS NOT OLD.width
  OR NEW.height IS NOT OLD.height
  OR NEW.blur_score IS NOT OLD.blur_score
  OR NEW.brightness IS NOT OLD.brightness
  OR NEW.quality_warnings_json IS NOT OLD.quality_warnings_json
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only order_index, is_cover and privacy_flags_json may change'); END;
CREATE TRIGGER listing_photos_no_delete BEFORE DELETE ON listing_photos
BEGIN SELECT RAISE(ABORT, 'photos may be referenced as evidence and are never deleted'); END;

-- Stronger than a plain foreign key: the evidence photo must belong to the SAME listing.
CREATE TRIGGER listing_facts_evidence_same_listing BEFORE INSERT ON listing_facts
WHEN NEW.evidence_photo_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM listing_photos
    WHERE id = NEW.evidence_photo_id AND listing_id = NEW.listing_id
)
BEGIN SELECT RAISE(ABORT, 'evidence photo must belong to the same listing'); END;

PRAGMA user_version = 2;
