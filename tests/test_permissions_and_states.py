import sqlite3

import pytest

from fakes import FakeLLMClient
from listing_assistant.agent_io import PhotoAnalysis
from listing_assistant.db.repositories import AgentRunRepository, AuditLogRepository
from listing_assistant.llm import (
    BudgetExceededError,
    LlmBudget,
    LLMOutputError,
    LLMRequest,
    TextPart,
)
from listing_assistant.models import AgentName, ListingStatus
from listing_assistant.state_machine import TRANSITIONS, TransitionError, transition
from listing_assistant.tools import TOOL_REGISTRY, AgentRun, PermissionDeniedError, summarize_args
from listing_assistant.toolset import ListingTools

A = AgentName
S = ListingStatus

# Architecture doc §5, permission matrix, transcribed independently of tools.py.
EXPECTED_MATRIX = {
    "get_listing_facts": {
        A.FACT_RECONCILER,
        A.GAP_DETECTOR,
        A.MARKET_ANALYST,
        A.COPYWRITER,
        A.SAFETY_REVIEWER,
    },
    "get_listing_photos": {A.INTAKE_GUARD, A.PHOTO_CURATOR, A.VISION_ANALYST},
    "vision_describe": {A.PHOTO_CURATOR, A.VISION_ANALYST},
    "find_duplicates": {A.PHOTO_CURATOR},
    "pii_scan_text": {A.INTAKE_GUARD, A.SAFETY_REVIEWER},
    "search_comparables": {A.MARKET_ANALYST},
    "compute_price_stats": {A.MARKET_ANALYST},
    "get_style_rules": {A.COPYWRITER, A.SAFETY_REVIEWER},
    "save_fact_proposals": {A.VISION_ANALYST, A.FACT_RECONCILER},
    "save_draft": {A.COPYWRITER},
    "request_user_input": {A.GAP_DETECTOR},
    "web_search": set(),
}


def make_run(conn, settings, schema, listing, agent):
    tools = ListingTools(conn, settings, schema, listing.id).as_mapping()
    return AgentRun(agent, listing.id, tools, FakeLLMClient(lambda r: None), LlmBudget(40, 0))


# --- permission matrix ----------------------------------------------------------------------


def test_registry_matches_the_architecture_document():
    assert {name: set(spec.allowed_agents) for name, spec in TOOL_REGISTRY.items()} == (
        EXPECTED_MATRIX
    )


def test_web_search_is_disabled():
    assert TOOL_REGISTRY["web_search"].enabled is False


def test_agents_reading_untrusted_content_cannot_write_listing_text():
    for agent in (A.VISION_ANALYST, A.PHOTO_CURATOR, A.INTAKE_GUARD, A.FACT_RECONCILER):
        assert agent not in TOOL_REGISTRY["save_draft"].allowed_agents


def test_copywriter_cannot_see_photos():
    for tool in ("get_listing_photos", "vision_describe"):
        assert A.COPYWRITER not in TOOL_REGISTRY[tool].allowed_agents


def test_no_tool_can_approve_export_delete_or_change_status():
    forbidden = ("approve", "export", "delete", "status", "transition", "listing_id")
    assert not [name for name in TOOL_REGISTRY if any(word in name for word in forbidden)]


@pytest.mark.parametrize("agent", list(AgentName))
@pytest.mark.parametrize("tool", sorted(TOOL_REGISTRY))
def test_gate_enforces_every_cell_of_the_matrix(conn, settings, schema, listing, agent, tool):
    run = make_run(conn, settings, schema, listing, agent)
    assert run.is_allowed(tool) == (agent in EXPECTED_MATRIX[tool])


def test_denied_and_unknown_calls_are_refused_and_persisted(conn, settings, schema, listing):
    run = make_run(conn, settings, schema, listing, A.MARKET_ANALYST)
    with pytest.raises(PermissionDeniedError):
        run.call("save_draft", content=None)
    with pytest.raises(PermissionDeniedError):
        run.call("delete_listing")
    AgentRunRepository(conn).save(run.to_record(), run.tool_calls)

    stored = AgentRunRepository(conn).tool_calls_for_run(run.id)
    assert [(c.tool_name, c.allowed) for c in stored] == [
        ("save_draft", False),
        ("delete_listing", False),
    ]


def test_failed_model_calls_still_count_towards_the_listing_cap(conn, settings, schema, listing):
    """Negative test: repeated failing calls cannot slip past the persisted per-listing cap."""
    failing = FakeLLMClient(lambda r: LLMOutputError("unusable"))
    runs = AgentRunRepository(conn)
    request = LLMRequest(
        system="s", parts=(TextPart("t"),), output_model=PhotoAnalysis, prompt_version="p"
    )
    for _ in range(3):
        used = runs.total_llm_calls(listing.id)
        tools = ListingTools(conn, settings, schema, listing.id).as_mapping()
        run = AgentRun(A.VISION_ANALYST, listing.id, tools, failing, LlmBudget(2, used))
        with pytest.raises((LLMOutputError, BudgetExceededError)):
            run.generate(request)
        runs.save(run.to_record(error="failed"), run.tool_calls)
    assert runs.total_llm_calls(listing.id) == 2
    assert len(failing.requests) == 2
    assert run.input_tokens == 0 and run.model is None


def test_tool_arguments_are_summarised_without_values():
    summary = summarize_args({"text": "TC 12345678901 tel 0532", "limit": 5})
    assert summary == "limit=int, text=str(len=23)"


def test_trace_tables_are_append_only(conn, settings, schema, listing):
    run = make_run(conn, settings, schema, listing, A.MARKET_ANALYST)
    run.call("get_listing_facts")
    AgentRunRepository(conn).save(run.to_record(), run.tool_calls)
    for table in ("agent_runs", "tool_calls"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), conn:
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 -- constant table names


# --- state machine --------------------------------------------------------------------------


def test_code_and_database_agree_on_allowed_transitions(conn):
    in_code = {(a.value, b.value) for a, targets in TRANSITIONS.items() for b in targets}
    in_db = set(conn.execute("SELECT from_status, to_status FROM allowed_transitions"))
    assert in_code == {tuple(row) for row in in_db}


def test_happy_path_transitions_are_audited(conn, listing):
    from listing_assistant.db.repositories import ApprovalRepository
    from listing_assistant.models import Approval, ApprovalDecision, ApprovalGate

    for target in (S.ANALYZING, S.FACTS_REVIEW):
        transition(conn, listing.id, target)
    # Leaving fact review requires the seller's gate-1 approval (migration 008).
    ApprovalRepository(conn).add(
        Approval(
            listing_id=listing.id,
            gate=ApprovalGate.FACTS,
            decision=ApprovalDecision.APPROVED,
            actor_name="local_user",
        )
    )
    transition(conn, listing.id, S.GENERATING)
    entries = AuditLogRepository(conn).list_for_resource(listing.id)
    assert [e.details["to"] for e in entries] == ["analyzing", "facts_review", "generating"]


@pytest.mark.parametrize("target", [S.APPROVED, S.EXPORTED, S.READY_FOR_APPROVAL, S.GENERATING])
def test_skipping_steps_is_refused(conn, listing, target):
    with pytest.raises(TransitionError):
        transition(conn, listing.id, target)


def test_database_refuses_illegal_transitions_even_with_raw_sql(conn, listing):
    with pytest.raises(sqlite3.IntegrityError, match="not allowed"), conn:
        conn.execute("UPDATE listings SET status = 'generating' WHERE id = ?", (listing.id,))


def test_transition_table_cannot_be_edited_at_runtime(conn):
    with pytest.raises(sqlite3.IntegrityError, match="fixed by migrations"), conn:
        conn.execute("INSERT INTO allowed_transitions VALUES ('draft', 'exported')")


def test_listings_cannot_be_deleted_or_recategorised(conn, listing):
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"), conn:
        conn.execute("DELETE FROM listings")
    with pytest.raises(sqlite3.IntegrityError, match="only listings.status"), conn:
        conn.execute("UPDATE listings SET category = 'car', created_at = 'x'")


def test_stale_status_loses_the_race(conn, listing):
    from listing_assistant.db.repositories import ListingRepository

    repo = ListingRepository(conn)
    assert repo.compare_and_set_status(listing.id, S.DRAFT, S.ANALYZING)
    assert not repo.compare_and_set_status(listing.id, S.DRAFT, S.ANALYZING)
