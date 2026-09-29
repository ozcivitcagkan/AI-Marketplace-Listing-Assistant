import sqlite3

import pytest

from listing_assistant.db.repositories import (
    AuditLogRepository,
    DraftRepository,
    FactRepository,
    ListingRepository,
    NotFoundError,
    PhotoRepository,
    SafetyReviewRepository,
)
from listing_assistant.models import (
    ActorType,
    AuditEntry,
    Claim,
    CopywriterOutput,
    Draft,
    Fact,
    FactSource,
    FactStatus,
    IssueSeverity,
    IssueType,
    Listing,
    ListingPhoto,
    ReviewerType,
    SafetyDecision,
    SafetyIssue,
    SafetyVerdict,
    new_id,
)


def user_fact(listing_id: str, key: str = "color", value: str = "kırmızı") -> Fact:
    return Fact(listing_id=listing_id, field_key=key, value=value, source=FactSource.USER)


def make_draft(listing_id: str, version: int, fact_id: str) -> Draft:
    return Draft(
        listing_id=listing_id,
        version=version,
        content=CopywriterOutput(
            title=Claim(text="Kırmızı sedan", fact_ids=[fact_id]),
            sentences=[Claim(text="Araç kırmızıdır.", fact_ids=[fact_id])],
        ),
        model="fake-model",
        prompt_version="copywriter-v1",
    )


# --- Listings ---------------------------------------------------------------------------


def test_listing_round_trip(conn):
    repo = ListingRepository(conn)
    listing = repo.add(Listing())
    assert repo.get(listing.id) == listing


@pytest.mark.parametrize("bad_id", [new_id(), "x' OR '1'='1"])
def test_unknown_or_malicious_listing_id_returns_none(conn, listing, bad_id):
    assert ListingRepository(conn).get(bad_id) is None


# --- Facts: always scoped to one listing ------------------------------------------------


def test_fact_round_trip_and_status_filter(conn, listing):
    repo = FactRepository(conn)
    color = repo.add(user_fact(listing.id))
    mileage = repo.add(user_fact(listing.id, "mileage_km", "120000"))
    repo.set_status(listing.id, color.id, FactStatus.APPROVED)

    assert repo.get(listing.id, color.id).status is FactStatus.APPROVED
    approved = repo.list_for_listing(listing.id, FactStatus.APPROVED)
    assert [f.id for f in approved] == [color.id]
    assert len(repo.list_for_listing(listing.id)) == 2
    assert repo.get(listing.id, mileage.id) == mileage


def test_facts_of_one_listing_are_invisible_to_another(conn, listing):
    other = ListingRepository(conn).add(Listing())
    repo = FactRepository(conn)
    fact = repo.add(user_fact(listing.id))

    assert repo.get(other.id, fact.id) is None
    assert repo.list_for_listing(other.id) == []
    with pytest.raises(NotFoundError):
        repo.set_status(other.id, fact.id, FactStatus.APPROVED)
    assert repo.get(listing.id, fact.id).status is FactStatus.PROPOSED


def add_photo_row(conn, listing_id: str) -> ListingPhoto:
    photo_id = new_id()
    return PhotoRepository(conn).add(
        ListingPhoto(
            id=photo_id,
            listing_id=listing_id,
            storage_key=f"{listing_id}/{photo_id}.jpg",
            sha256="0" * 64,
            perceptual_hash="0" * 16,
            width=800,
            height=600,
            blur_score=500.0,
            brightness=120.0,
            order_index=0,
        )
    )


def vision_fact(listing_id: str, photo_id: str) -> Fact:
    return Fact(
        listing_id=listing_id,
        field_key="body_type",
        value="suv",
        source=FactSource.VISION,
        confidence=0.8,
        evidence_photo_id=photo_id,
    )


def test_vision_fact_round_trip(conn, listing):
    photo = add_photo_row(conn, listing.id)
    repo = FactRepository(conn)
    fact = repo.add(vision_fact(listing.id, photo.id))
    assert repo.get(listing.id, fact.id) == fact


def test_vision_fact_cannot_cite_a_photo_of_another_listing(conn, listing):
    other = ListingRepository(conn).add(Listing())
    foreign_photo = add_photo_row(conn, other.id)
    with pytest.raises(sqlite3.IntegrityError, match="same listing"):
        FactRepository(conn).add(vision_fact(listing.id, foreign_photo.id))


def test_vision_fact_cannot_cite_a_missing_photo(conn, listing):
    with pytest.raises(sqlite3.IntegrityError, match="same listing"):
        FactRepository(conn).add(vision_fact(listing.id, new_id()))


# --- Drafts: versioned and immutable ----------------------------------------------------


def test_draft_versions(conn, listing):
    facts = FactRepository(conn)
    drafts = DraftRepository(conn)
    fact = facts.add(user_fact(listing.id))

    assert drafts.next_version(listing.id) == 1
    first = drafts.add(make_draft(listing.id, 1, fact.id))
    assert drafts.next_version(listing.id) == 2
    second = drafts.add(make_draft(listing.id, 2, fact.id))

    assert drafts.get(listing.id, 1) == first
    assert drafts.latest(listing.id) == second


def test_duplicate_draft_version_is_rejected(conn, listing):
    drafts = DraftRepository(conn)
    drafts.add(make_draft(listing.id, 1, new_id()))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        drafts.add(make_draft(listing.id, 1, new_id()))


# --- Safety reviews and audit log -------------------------------------------------------


def test_safety_review_stores_derived_decision(conn, listing):
    draft = DraftRepository(conn).add(make_draft(listing.id, 1, new_id()))
    repo = SafetyReviewRepository(conn)
    verdict = SafetyVerdict(
        draft_id=draft.id,
        reviewer_type=ReviewerType.CODE,
        issues=[
            SafetyIssue(
                issue_type=IssueType.SENSITIVE_INFO,
                severity=IssueSeverity.BLOCK,
                message="IBAN detected",
                claim_index=1,
            )
        ],
    )
    repo.add(verdict)

    stored = conn.execute("SELECT decision FROM safety_reviews").fetchone()["decision"]
    assert stored == SafetyDecision.BLOCK
    assert repo.list_for_draft(draft.id) == [verdict]


def test_audit_entry_round_trip(conn, listing):
    repo = AuditLogRepository(conn)
    entry = repo.record(
        AuditEntry(
            actor_type=ActorType.USER,
            actor_name="local_user",
            action="listing.create",
            resource_type="listing",
            resource_id=listing.id,
            details={"category": "car"},
        )
    )
    assert repo.list_for_resource(listing.id) == [entry]
