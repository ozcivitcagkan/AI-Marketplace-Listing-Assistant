-- Migration 004: a field can have at most ONE approved value per listing.
-- Approving a new value therefore requires rejecting the old one in the same transaction.

CREATE UNIQUE INDEX idx_listing_facts_one_approved_per_field
    ON listing_facts (listing_id, field_key)
    WHERE status = 'approved';

PRAGMA user_version = 4;
