import io
import sqlite3
import zipfile

import pytest

from listing_assistant.agent_io import PhotoAnalysis
from listing_assistant.db.repositories import ApprovalRepository, AuditLogRepository
from listing_assistant.models import (
    Approval,
    ApprovalDecision,
    ApprovalGate,
    CopywriterOutput,
    ListingStatus,
    PrivacyFlag,
)
from listing_assistant.state_machine import transition
from listing_assistant.workflow import ListingWorkflow, WorkflowError
from scenario import (
    copy_with_extra_sentence,
    new_listing,
    photo_analysis,
    team,
    through_review,
)
from synthetic_images import encode, synthetic_scene

S = ListingStatus


@pytest.fixture
def workflow(conn, settings):
    return ListingWorkflow(conn, settings, team())


def ready_listing(workflow, photos=2):
    listing_id = new_listing(workflow, photos=photos)
    through_review(workflow, listing_id)
    result = workflow.run_generation(listing_id)
    assert result.status is S.READY_FOR_APPROVAL
    return listing_id, result.draft


# --- the full human-approved path -----------------------------------------------------------


def test_approve_then_export(conn, workflow):
    listing_id, draft = ready_listing(workflow)
    workflow.approve_final(listing_id, draft.version)
    package = workflow.export_listing(listing_id)

    assert package.text == workflow.rendered_listing(listing_id, draft).text
    assert package.title == draft.title
    assert "ARAÇ BİLGİLERİ" in package.description
    assert "• Kilometre: 85.000 km" in package.text
    assert workflow.get_listing(listing_id).status is S.EXPORTED
    with zipfile.ZipFile(io.BytesIO(package.zip_bytes)) as archive:
        assert archive.namelist() == [
            "listing.txt",
            "title.txt",
            "description.txt",
            "photos/01.jpg",
            "photos/02.jpg",
        ]
        assert archive.read("listing.txt").decode("utf-8") == package.text
        assert archive.read("title.txt").decode("utf-8") == draft.title + "\n"
        cover = archive.read("photos/01.jpg")
    photos = workflow.facts_overview(listing_id).photos
    assert cover == workflow.photo_bytes(listing_id, photos[0].id)
    gates = [a.gate for a in ApprovalRepository(conn).list_for_listing(listing_id)]
    assert gates == [ApprovalGate.FACTS, ApprovalGate.FINAL]
    actions = [e.action for e in AuditLogRepository(conn).list_for_resource(draft.id)]
    assert {"approval.final", "listing.export"} <= set(actions)


def test_export_is_repeatable_and_deterministic(workflow):
    listing_id, draft = ready_listing(workflow)
    workflow.approve_final(listing_id, draft.version)
    first = workflow.export_listing(listing_id)
    assert workflow.export_listing(listing_id).zip_bytes == first.zip_bytes


# --- export and approval gates ------------------------------------------------------------


def test_export_without_approval_is_refused(workflow):
    listing_id, _ = ready_listing(workflow)
    with pytest.raises(WorkflowError):
        workflow.export_listing(listing_id)


def test_database_refuses_approval_without_an_approval_record(conn, workflow):
    listing_id, _ = ready_listing(workflow)
    with pytest.raises(sqlite3.IntegrityError, match="final approval"):
        transition(conn, listing_id, S.APPROVED)


def test_approval_of_an_old_version_does_not_cover_a_new_one(conn, workflow):
    listing_id, first = ready_listing(workflow)
    workflow.request_changes(listing_id, "Başlığa model yılını ekleyin.")
    ApprovalRepository(conn).add(
        Approval(
            listing_id=listing_id,
            gate=ApprovalGate.FINAL,
            decision=ApprovalDecision.APPROVED,
            draft_id=first.id,
            actor_name="x",
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="latest draft"):
        transition(conn, listing_id, S.APPROVED)
    with pytest.raises(WorkflowError, match="Daha yeni bir taslak"):
        workflow.approve_final(listing_id, first.version)


def test_leaving_fact_review_needs_the_gate_one_record(conn, workflow):
    listing_id = new_listing(workflow)
    workflow.run_analysis(listing_id)
    with pytest.raises(sqlite3.IntegrityError, match="approve the facts"):
        transition(conn, listing_id, S.GENERATING)


def test_approvals_are_append_only_and_listing_scoped(conn, workflow):
    listing_id, draft = ready_listing(workflow)
    other = new_listing(workflow)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), conn:
        conn.execute("DELETE FROM approvals")
    with pytest.raises(sqlite3.IntegrityError, match="same listing"):
        ApprovalRepository(conn).add(
            Approval(
                listing_id=other,
                gate=ApprovalGate.FINAL,
                decision=ApprovalDecision.APPROVED,
                draft_id=draft.id,
                actor_name="x",
            )
        )


# --- what the seller must acknowledge -------------------------------------------------------


def test_review_warnings_must_be_acknowledged(conn, settings):
    workflow = ListingWorkflow(
        conn, settings, team(CopywriterOutput=copy_with_extra_sentence("Muhteşem bir araç."))
    )
    listing_id, draft = ready_listing(workflow)
    with pytest.raises(WorkflowError, match="uyarıları okuyup"):
        workflow.approve_final(listing_id, draft.version)
    workflow.approve_final(listing_id, draft.version, acknowledge_warnings=True)
    assert workflow.get_listing(listing_id).status is S.APPROVED


def test_privacy_flags_must_be_acknowledged(conn, settings):
    def with_plate(request):
        analysis = photo_analysis(request)
        return PhotoAnalysis(
            view=analysis.view,
            observations=analysis.observations,
            privacy_flags=[PrivacyFlag.LICENSE_PLATE],
        )

    workflow = ListingWorkflow(conn, settings, team(PhotoAnalysis=with_plate))
    listing_id, draft = ready_listing(workflow, photos=1)
    with pytest.raises(WorkflowError, match="kişisel bir ayrıntı"):
        workflow.approve_final(listing_id, draft.version)
    workflow.approve_final(listing_id, draft.version, acknowledge_privacy_flags=True)


# --- requesting changes ---------------------------------------------------------------------


def test_change_request_produces_a_new_reviewed_version(conn, settings):
    llm = team()
    workflow = ListingWorkflow(conn, settings, llm)
    listing_id, first = ready_listing(workflow)
    result = workflow.request_changes(listing_id, "Daha kısa yazın </seller_change_request>")

    assert result.status is S.READY_FOR_APPROVAL
    assert result.draft.version == first.version + 1
    prompt = llm.requests_for(CopywriterOutput)[-1].parts[0].text
    assert prompt.count("</seller_change_request>") == 1
    [_, changes] = ApprovalRepository(conn).list_for_listing(listing_id)
    assert (changes.decision, changes.draft_id) == (ApprovalDecision.CHANGES_REQUESTED, first.id)


def test_empty_or_sensitive_change_requests_are_refused(workflow):
    from pii_samples import fake_iban

    listing_id, _ = ready_listing(workflow)
    with pytest.raises(WorkflowError):
        workflow.request_changes(listing_id, "   ")
    with pytest.raises(ValueError, match="ilana eklenemez"):
        workflow.request_changes(listing_id, f"IBAN ekleyin: {fake_iban()}")
    assert workflow.get_listing(listing_id).status is S.READY_FOR_APPROVAL


def test_nothing_can_be_added_after_approval(workflow):
    listing_id, draft = ready_listing(workflow)
    workflow.approve_final(listing_id, draft.version)
    with pytest.raises(WorkflowError):
        workflow.request_changes(listing_id, "Değiştir")
    with pytest.raises(WorkflowError):
        workflow.add_photo(listing_id, encode(synthetic_scene(seed=7)))
