-- Migration 001: core listing tables and append-only protections.
-- Never edit a migration after it has been applied; add a new numbered file instead.

CREATE TABLE listings (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL CHECK (category IN ('car')),
    status      TEXT NOT NULL CHECK (status IN (
                    'draft', 'analyzing', 'facts_review', 'needs_info', 'generating',
                    'safety_check', 'blocked', 'moderation', 'ready_for_approval',
                    'approved', 'exported')),
    created_at  TEXT NOT NULL
);

CREATE TABLE listing_facts (
    id                 TEXT PRIMARY KEY,
    listing_id         TEXT NOT NULL REFERENCES listings (id),
    field_key          TEXT NOT NULL,
    value              TEXT NOT NULL,
    source             TEXT NOT NULL CHECK (source IN ('user', 'vision', 'comparable', 'computed')),
    status             TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
    confidence         REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    -- The foreign key to listing_photos is added with the photos table (migration 002).
    evidence_photo_id  TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX idx_listing_facts_listing ON listing_facts (listing_id);

CREATE TABLE drafts (
    id              TEXT PRIMARY KEY,
    listing_id      TEXT NOT NULL REFERENCES listings (id),
    version         INTEGER NOT NULL CHECK (version >= 1),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (listing_id, version)
);

CREATE TABLE safety_reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id       TEXT NOT NULL REFERENCES drafts (id),
    reviewer_type  TEXT NOT NULL CHECK (reviewer_type IN ('code', 'llm')),
    decision       TEXT NOT NULL CHECK (decision IN ('pass', 'warn', 'block')),
    issues_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX idx_safety_reviews_draft ON safety_reviews (draft_id);

CREATE TABLE audit_logs (
    id             TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
    actor_name     TEXT NOT NULL,
    action         TEXT NOT NULL,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT,
    details_json   TEXT NOT NULL
);
CREATE INDEX idx_audit_logs_resource ON audit_logs (resource_id);

-- Append-only tables: enforced by the database, so a bug in Python cannot bypass it.
CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;

CREATE TRIGGER drafts_no_update BEFORE UPDATE ON drafts
BEGIN SELECT RAISE(ABORT, 'drafts are immutable; insert a new version'); END;
CREATE TRIGGER drafts_no_delete BEFORE DELETE ON drafts
BEGIN SELECT RAISE(ABORT, 'drafts are immutable; insert a new version'); END;

CREATE TRIGGER safety_reviews_no_update BEFORE UPDATE ON safety_reviews
BEGIN SELECT RAISE(ABORT, 'safety_reviews is append-only'); END;
CREATE TRIGGER safety_reviews_no_delete BEFORE DELETE ON safety_reviews
BEGIN SELECT RAISE(ABORT, 'safety_reviews is append-only'); END;

-- A fact's content never changes; a correction is a new fact. Only the review status moves.
CREATE TRIGGER listing_facts_only_status_changes BEFORE UPDATE ON listing_facts
WHEN NEW.id IS NOT OLD.id
  OR NEW.listing_id IS NOT OLD.listing_id
  OR NEW.field_key IS NOT OLD.field_key
  OR NEW.value IS NOT OLD.value
  OR NEW.source IS NOT OLD.source
  OR NEW.confidence IS NOT OLD.confidence
  OR NEW.evidence_photo_id IS NOT OLD.evidence_photo_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only listing_facts.status may change'); END;
CREATE TRIGGER listing_facts_no_delete BEFORE DELETE ON listing_facts
BEGIN SELECT RAISE(ABORT, 'facts are never deleted; reject them instead'); END;

PRAGMA user_version = 1;
