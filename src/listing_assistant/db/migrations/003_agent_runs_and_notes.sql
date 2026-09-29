-- Migration 003: agent traces (agent_runs, tool_calls) and the seller's free-text notes.

CREATE TABLE agent_runs (
    id              TEXT PRIMARY KEY,
    listing_id      TEXT NOT NULL REFERENCES listings (id),
    agent_name      TEXT NOT NULL CHECK (agent_name IN (
                        'intake_guard', 'photo_curator', 'vision_analyst', 'fact_reconciler',
                        'gap_detector', 'market_analyst', 'copywriter', 'safety_reviewer')),
    status          TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    model           TEXT,
    prompt_version  TEXT,
    llm_calls       INTEGER NOT NULL CHECK (llm_calls >= 0),
    input_tokens    INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens   INTEGER NOT NULL CHECK (output_tokens >= 0),
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
CREATE INDEX idx_agent_runs_listing ON agent_runs (listing_id);

-- Every tool call, allowed or refused. Arguments are summarised (types/sizes), never stored.
CREATE TABLE tool_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_run_id  TEXT NOT NULL REFERENCES agent_runs (id),
    tool_name     TEXT NOT NULL,
    allowed       INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    args_summary  TEXT NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_tool_calls_run ON tool_calls (agent_run_id);

-- Untrusted seller text. Kept for review; never cited by the Copywriter directly.
CREATE TABLE listing_notes (
    listing_id  TEXT PRIMARY KEY REFERENCES listings (id),
    notes       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TRIGGER agent_runs_no_update BEFORE UPDATE ON agent_runs
BEGIN SELECT RAISE(ABORT, 'agent_runs is append-only'); END;
CREATE TRIGGER agent_runs_no_delete BEFORE DELETE ON agent_runs
BEGIN SELECT RAISE(ABORT, 'agent_runs is append-only'); END;
CREATE TRIGGER tool_calls_no_update BEFORE UPDATE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;
CREATE TRIGGER tool_calls_no_delete BEFORE DELETE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;
CREATE TRIGGER listing_notes_no_update BEFORE UPDATE ON listing_notes
BEGIN SELECT RAISE(ABORT, 'listing_notes is immutable'); END;
CREATE TRIGGER listing_notes_no_delete BEFORE DELETE ON listing_notes
BEGIN SELECT RAISE(ABORT, 'listing_notes is immutable'); END;

PRAGMA user_version = 3;
