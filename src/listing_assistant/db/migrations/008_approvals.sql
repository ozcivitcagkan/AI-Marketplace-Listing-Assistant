-- Migration 008: human approvals, and the database-level gates that depend on them.

CREATE TABLE approvals (
    id           TEXT PRIMARY KEY,
    listing_id   TEXT NOT NULL REFERENCES listings (id),
    gate         TEXT NOT NULL CHECK (gate IN ('facts', 'final')),
    decision     TEXT NOT NULL CHECK (decision IN ('approved', 'changes_requested')),
    draft_id     TEXT REFERENCES drafts (id),
    comment      TEXT,
    actor_name   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    CHECK ((gate = 'final') = (draft_id IS NOT NULL))
);
CREATE INDEX idx_approvals_listing ON approvals (listing_id);

CREATE TRIGGER approvals_no_update BEFORE UPDATE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;

CREATE TRIGGER approvals_draft_same_listing BEFORE INSERT ON approvals
WHEN NEW.draft_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM drafts WHERE id = NEW.draft_id AND listing_id = NEW.listing_id
)
BEGIN SELECT RAISE(ABORT, 'approved draft must belong to the same listing'); END;

-- Gate 1: leaving fact review requires a recorded facts approval.
CREATE TRIGGER listings_facts_approval_required BEFORE UPDATE OF status ON listings
WHEN OLD.status = 'facts_review' AND NOT EXISTS (
    SELECT 1 FROM approvals
    WHERE listing_id = NEW.id AND gate = 'facts' AND decision = 'approved'
)
BEGIN SELECT RAISE(ABORT, 'the seller must approve the facts first'); END;

-- Gate 2: approved/exported require a final approval of the LATEST draft version.
-- An approval of version 1 does not cover a version 2 written afterwards.
CREATE TRIGGER listings_final_approval_required BEFORE UPDATE OF status ON listings
WHEN NEW.status IN ('approved', 'exported') AND NOT EXISTS (
    SELECT 1 FROM approvals a
    WHERE a.listing_id = NEW.id AND a.gate = 'final' AND a.decision = 'approved'
      AND a.draft_id = (
          SELECT d.id FROM drafts d WHERE d.listing_id = NEW.id ORDER BY d.version DESC LIMIT 1
      )
)
BEGIN SELECT RAISE(ABORT, 'final approval of the latest draft is required'); END;

PRAGMA user_version = 8;
