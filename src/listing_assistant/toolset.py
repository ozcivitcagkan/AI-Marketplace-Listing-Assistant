"""Tool implementations, bound to exactly one listing (architecture doc §5, scope binding).

`ListingTools(conn, ..., listing_id)` closes over the listing ID. No method accepts a
listing ID, and every object passed in is checked against the bound listing, so an
agent that is tricked into "fetching another listing" has no way to express it.
"""

import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from listing_assistant.agent_io import PhotoAnalysis
from listing_assistant.claims import UnsupportedClaimError, unknown_fact_ids
from listing_assistant.config import Settings
from listing_assistant.db.repositories import (
    ClarificationRepository,
    ComparableRepository,
    DraftRepository,
    FactRepository,
    PhotoRepository,
)
from listing_assistant.field_schema import CategorySchema, FieldDefinition
from listing_assistant.images import find_near_duplicates, prepare_for_model
from listing_assistant.llm import ImagePart, LLMRequest, TextPart
from listing_assistant.models import (
    Clarification,
    CopywriterOutput,
    Draft,
    Fact,
    FactStatus,
    ListingPhoto,
)
from listing_assistant.photo_intake import photo_path
from listing_assistant.pii import PiiMatch, scan_text
from listing_assistant.policy import style_rules_text
from listing_assistant.prompting import load_prompt
from listing_assistant.text_utils import place_key
from listing_assistant.tools import AgentRun, PermissionDeniedError

VISION_PROMPT = "vision_analyst_v1"
# Below this many comparables no range is shown at all (architecture doc §9.1).
MIN_SAMPLE_SIZE = 5


@dataclass(frozen=True)
class PriceStats:
    sample_size: int
    q1: int
    median: int
    q3: int


def price_stats(prices: list[int]) -> PriceStats | None:
    """Median and quartiles, computed by code; None when the sample is too small."""
    if len(prices) < MIN_SAMPLE_SIZE:
        return None
    q1, median, q3 = statistics.quantiles(prices, n=4, method="inclusive")
    return PriceStats(len(prices), round(q1), round(median), round(q3))


@dataclass(frozen=True)
class PhotoWithBytes:
    photo: ListingPhoto
    jpeg_bytes: bytes = field(repr=False)


def describe_field(definition: FieldDefinition) -> str:
    """One trusted line about a field, generated from our own schema file."""
    line = f"- {definition.key} ({definition.value_type.value}"
    if definition.choices:
        line += ": one of " + ", ".join(definition.choices)
    line += ")"
    if definition.note:
        line += f": {definition.note}"
    return line


class ListingTools:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        schema: CategorySchema,
        listing_id: str,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._schema = schema
        self.listing_id = listing_id

    def as_mapping(self) -> dict[str, Callable[..., Any]]:
        return {
            "get_listing_facts": self.get_listing_facts,
            "get_listing_photos": self.get_listing_photos,
            "vision_describe": self.vision_describe,
            "find_duplicates": self.find_duplicates,
            "save_fact_proposals": self.save_fact_proposals,
            "request_user_input": self.request_user_input,
            "get_style_rules": self.get_style_rules,
            "save_draft": self.save_draft,
            "pii_scan_text": self.pii_scan_text,
            "search_comparables": self.search_comparables,
            "compute_price_stats": self.compute_price_stats,
        }

    # --- read tools ---------------------------------------------------------------------

    def get_listing_facts(
        self, run: AgentRun, status: FactStatus | None = FactStatus.APPROVED
    ) -> list[Fact]:
        return FactRepository(self._conn).list_for_listing(self.listing_id, status)

    def get_listing_photos(self, run: AgentRun) -> list[PhotoWithBytes]:
        photos = PhotoRepository(self._conn).list_for_listing(self.listing_id)
        return [
            PhotoWithBytes(p, photo_path(self._settings, p.storage_key).read_bytes())
            for p in photos
        ]

    def find_duplicates(self, run: AgentRun, photos: list[ListingPhoto]) -> list[tuple]:
        self._check_scope(p.listing_id for p in photos)
        return find_near_duplicates([(p.id, p.perceptual_hash) for p in photos])

    def search_comparables(
        self,
        run: AgentRun,
        make: str,
        model: str,
        year_min: int,
        year_max: int,
        city: str | None,
    ) -> list[int]:
        rows = ComparableRepository(self._conn).search(make, model, year_min, year_max)
        wanted = place_key(city) if city else None
        return [price for row_city, price in rows if wanted in (None, place_key(row_city))]

    def compute_price_stats(self, run: AgentRun, prices: list[int]) -> PriceStats | None:
        return price_stats(prices)

    def pii_scan_text(self, run: AgentRun, text: str) -> list[PiiMatch]:
        return scan_text(text)

    def get_style_rules(self, run: AgentRun) -> str:
        return style_rules_text()

    def vision_describe(self, run: AgentRun, photo: PhotoWithBytes) -> PhotoAnalysis:
        self._check_scope([photo.photo.listing_id])
        allowed = [self._schema.get(key) for key in self._schema.photo_observable_keys]
        prompt = load_prompt(VISION_PROMPT)
        request = LLMRequest(
            system=prompt.text,
            parts=(
                ImagePart(prepare_for_model(photo.jpeg_bytes)),
                TextPart("Allowed fields:\n" + "\n".join(describe_field(d) for d in allowed)),
            ),
            output_model=PhotoAnalysis,
            prompt_version=prompt.version,
        )
        return run.generate(request)

    # --- draft-write tools --------------------------------------------------------------

    def save_fact_proposals(self, run: AgentRun, facts: list[Fact]) -> list[Fact]:
        """Agents may only PROPOSE facts; approval is a human action elsewhere."""
        self._check_scope(f.listing_id for f in facts)
        if any(f.status is not FactStatus.PROPOSED for f in facts):
            raise PermissionDeniedError("agents can only save facts with status 'proposed'")
        repo = FactRepository(self._conn)
        return [repo.add(fact) for fact in facts]

    def save_draft(self, run: AgentRun, content: CopywriterOutput) -> Draft:
        """Store a new draft version, but only if every claim cites approved facts of this
        listing. Otherwise nothing is stored (architecture doc §7.1, code verification)."""
        approved = {f.id for f in self.get_listing_facts(run, FactStatus.APPROVED)}
        unknown = unknown_fact_ids(content, approved)
        if unknown:
            raise UnsupportedClaimError(unknown)
        drafts = DraftRepository(self._conn)
        return drafts.add(
            Draft(
                listing_id=self.listing_id,
                version=drafts.next_version(self.listing_id),
                content=content,
                model=run.model or "unknown",
                prompt_version=run.prompt_version or "unknown",
            )
        )

    # --- flow tools -------------------------------------------------------------------

    def request_user_input(self, run: AgentRun, questions: dict[str, str]) -> list[Clarification]:
        """Record questions for the seller; the orchestrator pauses the flow on them."""
        for key in questions:
            self._schema.get(key)  # only real fields can be asked about
        repo = ClarificationRepository(self._conn)
        return [
            repo.add(Clarification(listing_id=self.listing_id, field_key=key, question=text))
            for key, text in questions.items()
        ]

    def _check_scope(self, listing_ids) -> None:
        if any(listing_id != self.listing_id for listing_id in listing_ids):
            raise PermissionDeniedError("object belongs to a different listing")
