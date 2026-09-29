-- Migration 009: the seller can recover a blocked draft instead of starting over.
--   blocked            -> facts_review : correct or remove the facts behind the problem
--   blocked            -> generating   : rewrite with the seller's change request
--   ready_for_approval -> facts_review : go back and change facts before approving
-- The workflow records a fresh gate-1 approval every time the seller finishes a review,
-- and gate 2 still binds to the latest draft, so a draft written after the seller went
-- back is never covered by an earlier final approval.

-- The table is locked against inserts; lift the lock only inside this migration.
DROP TRIGGER allowed_transitions_no_insert;
INSERT INTO allowed_transitions (from_status, to_status) VALUES
    ('blocked', 'facts_review'),
    ('blocked', 'generating'),
    ('ready_for_approval', 'facts_review');
CREATE TRIGGER allowed_transitions_no_insert BEFORE INSERT ON allowed_transitions
BEGIN SELECT RAISE(ABORT, 'allowed_transitions is fixed by migrations'); END;

PRAGMA user_version = 9;
