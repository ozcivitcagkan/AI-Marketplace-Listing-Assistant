-- Migration 007: the listing state machine, enforced by the database (architecture doc §2).
-- listing_assistant.state_machine holds the same table; a test keeps the two identical.

CREATE TABLE allowed_transitions (
    from_status  TEXT NOT NULL,
    to_status    TEXT NOT NULL,
    PRIMARY KEY (from_status, to_status)
);
INSERT INTO allowed_transitions (from_status, to_status) VALUES
    ('draft', 'analyzing'),
    ('analyzing', 'facts_review'),
    ('facts_review', 'needs_info'),
    ('facts_review', 'generating'),
    ('needs_info', 'generating'),
    ('generating', 'safety_check'),
    ('safety_check', 'generating'),
    ('safety_check', 'blocked'),
    ('safety_check', 'ready_for_approval'),
    ('blocked', 'moderation'),
    ('moderation', 'generating'),
    ('ready_for_approval', 'generating'),
    ('ready_for_approval', 'approved'),
    ('approved', 'exported');

CREATE TRIGGER allowed_transitions_no_update BEFORE UPDATE ON allowed_transitions
BEGIN SELECT RAISE(ABORT, 'allowed_transitions is fixed by migrations'); END;
CREATE TRIGGER allowed_transitions_no_delete BEFORE DELETE ON allowed_transitions
BEGIN SELECT RAISE(ABORT, 'allowed_transitions is fixed by migrations'); END;
CREATE TRIGGER allowed_transitions_no_insert BEFORE INSERT ON allowed_transitions
BEGIN SELECT RAISE(ABORT, 'allowed_transitions is fixed by migrations'); END;

CREATE TRIGGER listings_status_transition_allowed BEFORE UPDATE OF status ON listings
WHEN NOT EXISTS (
    SELECT 1 FROM allowed_transitions WHERE from_status = OLD.status AND to_status = NEW.status
)
BEGIN SELECT RAISE(ABORT, 'listing status transition is not allowed'); END;

CREATE TRIGGER listings_only_status_changes BEFORE UPDATE ON listings
WHEN NEW.id IS NOT OLD.id
  OR NEW.category IS NOT OLD.category
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only listings.status may change'); END;

CREATE TRIGGER listings_no_delete BEFORE DELETE ON listings
BEGIN SELECT RAISE(ABORT, 'listings are never deleted'); END;

PRAGMA user_version = 7;
