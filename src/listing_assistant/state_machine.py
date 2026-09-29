"""The listing state machine (architecture doc §2). The ONLY code that changes a status.

Transitions are checked twice: here against `TRANSITIONS`, and in the database by the
trigger from migration 007. The update is a compare-and-set (`WHERE status = <old>`), so
two concurrent actions cannot both move the same listing.
"""

import sqlite3

from listing_assistant.db.repositories import AuditLogRepository, ListingRepository, NotFoundError
from listing_assistant.models import ActorType, AuditEntry, Listing, ListingStatus

S = ListingStatus

TRANSITIONS: dict[ListingStatus, frozenset[ListingStatus]] = {
    S.DRAFT: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.FACTS_REVIEW}),
    S.FACTS_REVIEW: frozenset({S.NEEDS_INFO, S.GENERATING}),
    S.NEEDS_INFO: frozenset({S.GENERATING}),
    S.GENERATING: frozenset({S.SAFETY_CHECK}),
    S.SAFETY_CHECK: frozenset({S.GENERATING, S.BLOCKED, S.READY_FOR_APPROVAL}),
    # Migration 009: the seller recovers a blocked draft by fixing facts or rewriting.
    S.BLOCKED: frozenset({S.MODERATION, S.FACTS_REVIEW, S.GENERATING}),
    S.MODERATION: frozenset({S.GENERATING}),
    S.READY_FOR_APPROVAL: frozenset({S.GENERATING, S.APPROVED, S.FACTS_REVIEW}),
    S.APPROVED: frozenset({S.EXPORTED}),
    S.EXPORTED: frozenset(),
}


class TransitionError(Exception):
    pass


def can_transition(current: ListingStatus, target: ListingStatus) -> bool:
    return target in TRANSITIONS[current]


def transition(
    conn: sqlite3.Connection,
    listing_id: str,
    target: ListingStatus,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_name: str = "orchestrator",
) -> Listing:
    listings = ListingRepository(conn)
    listing = listings.get(listing_id)
    if listing is None:
        raise NotFoundError(f"listing {listing_id} not found")
    if not can_transition(listing.status, target):
        raise TransitionError(f"cannot move a listing from {listing.status} to {target}")
    if not listings.compare_and_set_status(listing_id, listing.status, target):
        raise TransitionError("the listing changed concurrently; reload and try again")
    AuditLogRepository(conn).record(
        AuditEntry(
            actor_type=actor_type,
            actor_name=actor_name,
            action="listing.transition",
            resource_type="listing",
            resource_id=listing_id,
            details={"from": listing.status.value, "to": target.value},
        )
    )
    return listings.get(listing_id)
