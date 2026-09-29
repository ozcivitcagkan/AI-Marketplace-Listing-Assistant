"""The seller fixes a problem and continues, instead of starting a new listing (migration 009)."""

import sqlite3

import pytest

from listing_assistant.agents.safety_reviewer import combined_decision
from listing_assistant.db.repositories import ApprovalRepository, AuditLogRepository
from listing_assistant.models import (
    ApprovalGate,
    Claim,
    CopywriterOutput,
    ListingSection,
    ListingStatus,
    SafetyDecision,
)
from listing_assistant.state_machine import transition
from listing_assistant.workflow import ListingWorkflow, WorkflowError
from scenario import (
    FORM,
    answer_all,
    approve_all,
    approved_facts_in,
    honest_copy,
    new_listing,
    team,
    through_review,
)

S = ListingStatus


def with_sentence(text, field, section=ListingSection.CONDITION):
    """Honest copy plus one sentence that cites `field` when it exists, else the make."""

    def write(request):
        honest = honest_copy(request)
        by_field = {f["field"]: f["fact_id"] for f in approved_facts_in(request)}
        cited = by_field.get(field, by_field["make"])
        extra = Claim(text=text, fact_ids=[cited], section=section)
        return CopywriterOutput(title=honest.title, sentences=[*honest.sentences, extra])

    return write


def blocked_listing(workflow):
    listing_id = new_listing(workflow)
    through_review(workflow, listing_id)
    result = workflow.run_generation(listing_id)
    assert result.status is S.BLOCKED
    return listing_id


# --- the false positive from the real run --------------------------------------------------


def test_seller_absence_statement_is_a_warning_not_a_block(conn, settings):
    """'Görünür Hasar: yok' typed by the seller supports "görünür hasar yok"."""
    llm = team(CopywriterOutput=with_sentence("Aracımda görünür hasar yok.", "visible_damage"))
    workflow = ListingWorkflow(conn, settings, llm)
    listing_id = new_listing(workflow, fields={**FORM, "visible_damage": "yok"})
    through_review(workflow, listing_id)

    result = workflow.run_generation(listing_id)
    assert result.status is S.READY_FOR_APPROVAL
    assert result.correction_rounds == 0
    assert combined_decision(list(result.verdicts)) is SafetyDecision.WARN


def test_invented_absence_claim_is_still_blocked(conn, settings):
    """Negative test: without the seller's statement the same sentence stays blocked."""
    llm = team(CopywriterOutput=with_sentence("Aracımda görünür hasar yok.", "visible_damage"))
    workflow = ListingWorkflow(conn, settings, llm)
    assert workflow.get_listing(blocked_listing(workflow)).status is S.BLOCKED


# --- recovery path 1: go back to the facts --------------------------------------------------


def test_blocked_listing_recovers_by_fixing_facts_and_exports(conn, settings):
    llm = team(CopywriterOutput=with_sentence("Aracım kazasız.", "accident_history"))
    workflow = ListingWorkflow(conn, settings, llm)
    listing_id = blocked_listing(workflow)
    blocked_version = workflow.latest_draft(listing_id)[0].version

    assert workflow.reopen_facts(listing_id) is S.FACTS_REVIEW
    workflow.set_seller_fact(listing_id, "accident_history", "kazasız")
    assert workflow.complete_fact_review(listing_id) is S.GENERATING

    result = workflow.run_generation(listing_id)
    assert result.status is S.READY_FOR_APPROVAL
    assert result.draft.version > blocked_version
    workflow.approve_final(listing_id, result.draft.version, acknowledge_warnings=True)
    package = workflow.export_listing(listing_id)
    assert "Aracım kazasız." in package.description
    assert workflow.get_listing(listing_id).status is S.EXPORTED

    gates = [a.gate for a in ApprovalRepository(conn).list_for_listing(listing_id)]
    assert gates.count(ApprovalGate.FACTS) == 2  # gate 1 is recorded again after going back
    actions = [e.action for e in AuditLogRepository(conn).list_for_resource(listing_id)]
    assert "listing.reopen_facts" in actions


# --- recovery path 2: instruct the writer ----------------------------------------------------


def test_blocked_listing_recovers_with_a_change_request(conn, settings):
    bad = with_sentence("Aracım kazasız.", "accident_history")

    def writer(request):
        text = request.parts[0].text
        return honest_copy(request) if "seller_change_request" in text else bad(request)

    workflow = ListingWorkflow(conn, settings, team(CopywriterOutput=writer))
    listing_id = blocked_listing(workflow)

    result = workflow.request_changes(listing_id, "Kaza ile ilgili cümleyi çıkar.")
    assert result.status is S.READY_FOR_APPROVAL
    assert "kazasız" not in result.draft.description


def test_change_request_after_a_block_is_still_screened(conn, settings):
    llm = team(CopywriterOutput=with_sentence("Aracım kazasız.", "accident_history"))
    workflow = ListingWorkflow(conn, settings, llm)
    listing_id = blocked_listing(workflow)
    with pytest.raises(WorkflowError):
        workflow.request_changes(listing_id, "   ")
    assert workflow.get_listing(listing_id).status is S.BLOCKED


# --- going back before approval ---------------------------------------------------------------


def test_going_back_from_approval_needs_a_fresh_approval_of_the_new_draft(conn, settings):
    workflow = ListingWorkflow(conn, settings, team())
    listing_id = new_listing(workflow)
    through_review(workflow, listing_id)
    first = workflow.run_generation(listing_id).draft

    workflow.reopen_facts(listing_id)
    workflow.set_seller_fact(listing_id, "city", "İzmir")
    workflow.complete_fact_review(listing_id)
    second = workflow.run_generation(listing_id).draft

    assert second.version == first.version + 1
    with pytest.raises(WorkflowError, match="Daha yeni bir taslak"):
        workflow.approve_final(listing_id, first.version)
    workflow.approve_final(listing_id, second.version)
    assert workflow.get_listing(listing_id).status is S.APPROVED


def test_reopen_is_refused_outside_blocked_or_approval(conn, settings):
    workflow = ListingWorkflow(conn, settings, team())
    listing_id = new_listing(workflow)
    with pytest.raises(WorkflowError):
        workflow.reopen_facts(listing_id)  # draft
    workflow.run_analysis(listing_id)
    with pytest.raises(WorkflowError):
        workflow.reopen_facts(listing_id)  # already in fact review
    approve_all(workflow, listing_id)
    workflow.complete_fact_review(listing_id)
    answer_all(workflow, listing_id, {"model_year": "2019", "mileage_km": "85000"})
    assert workflow.get_listing(listing_id).status is S.GENERATING
    with pytest.raises(WorkflowError):
        workflow.reopen_facts(listing_id)


def test_approved_listing_cannot_go_back(conn, settings):
    """Negative test: after the final approval the text is fixed; the database refuses."""
    workflow = ListingWorkflow(conn, settings, team())
    listing_id = new_listing(workflow)
    through_review(workflow, listing_id)
    draft = workflow.run_generation(listing_id).draft
    workflow.approve_final(listing_id, draft.version)
    with pytest.raises(WorkflowError):
        workflow.reopen_facts(listing_id)
    with pytest.raises(Exception, match="cannot move"):
        transition(conn, listing_id, S.FACTS_REVIEW)
    with pytest.raises(sqlite3.IntegrityError, match="not allowed"), conn:
        conn.execute("UPDATE listings SET status = 'facts_review' WHERE id = ?", (listing_id,))
