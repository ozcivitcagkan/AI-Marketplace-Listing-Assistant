"""Data models exchanged between agents, services, and storage.

Every model rejects unknown fields and is immutable. Agent output that does not fit
these shapes is discarded, never repaired (architecture doc §4, §7.2).
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


# A strict ID format means a model-supplied value like "../other" can never pass as an ID.
Id = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
FieldKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Category(StrEnum):
    CAR = "car"


class ListingStatus(StrEnum):
    DRAFT = "draft"
    ANALYZING = "analyzing"
    FACTS_REVIEW = "facts_review"
    NEEDS_INFO = "needs_info"
    GENERATING = "generating"
    SAFETY_CHECK = "safety_check"
    BLOCKED = "blocked"
    MODERATION = "moderation"
    READY_FOR_APPROVAL = "ready_for_approval"
    APPROVED = "approved"
    EXPORTED = "exported"


# What the seller reads for each status.
STATUS_LABELS_TR: dict[ListingStatus, str] = {
    ListingStatus.DRAFT: "Fotoğraf bekliyor",
    ListingStatus.ANALYZING: "Analiz ediliyor",
    ListingStatus.FACTS_REVIEW: "Bilgi kontrolü",
    ListingStatus.NEEDS_INFO: "Cevap bekliyor",
    ListingStatus.GENERATING: "Yazılmaya hazır",
    ListingStatus.SAFETY_CHECK: "Kontrol ediliyor",
    ListingStatus.BLOCKED: "Düzeltme gerekiyor",
    ListingStatus.MODERATION: "İncelemede",
    ListingStatus.READY_FOR_APPROVAL: "Onay bekliyor",
    ListingStatus.APPROVED: "Onaylandı",
    ListingStatus.EXPORTED: "Dışa aktarıldı",
}


class FactSource(StrEnum):
    USER = "user"
    VISION = "vision"
    COMPARABLE = "comparable"
    COMPUTED = "computed"


class FactStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class Listing(StrictModel):
    id: Id = Field(default_factory=new_id)
    category: Category = Category.CAR
    status: ListingStatus = ListingStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)


class VisionFactProposal(StrictModel):
    """One observation the Vision Analyst may return.

    It has no `source`, `status`, or `listing_id` on purpose: the model cannot claim a
    fact came from the user, mark it approved, or aim it at another listing.
    """

    field_key: FieldKey
    value: str = Field(min_length=1, max_length=200)
    confidence: Confidence
    evidence_photo_id: Id


class Fact(StrictModel):
    """One piece of information about a listing: the single source of truth."""

    id: Id = Field(default_factory=new_id)
    listing_id: Id
    field_key: FieldKey
    value: str = Field(min_length=1, max_length=500)
    source: FactSource
    status: FactStatus = FactStatus.PROPOSED
    confidence: Confidence | None = None
    evidence_photo_id: Id | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check_evidence_rules(self) -> "Fact":
        if self.source is FactSource.VISION:
            if self.evidence_photo_id is None or self.confidence is None:
                raise ValueError("vision facts require evidence_photo_id and confidence")
        elif self.evidence_photo_id is not None:
            raise ValueError("only vision facts can reference an evidence photo")
        return self


class ListingSection(StrEnum):
    """Headed parts of the listing description; headings are fixed Turkish text in code."""

    OVERVIEW = "overview"
    APPEARANCE = "appearance"
    CONDITION = "condition"
    MAINTENANCE = "maintenance"
    SALE = "sale"


class Claim(StrictModel):
    """One sentence of listing text plus the facts that justify it."""

    text: str = Field(min_length=1, max_length=500)
    fact_ids: list[Id] = Field(min_length=1, max_length=20)
    # Defaulted so drafts stored before sections existed still load unchanged.
    section: ListingSection = ListingSection.OVERVIEW


class CopywriterOutput(StrictModel):
    """What the Copywriter may return.

    There is no free-text description field: code builds the description from
    sourced sentences, so an unsourced sentence cannot even be expressed.
    """

    title: Claim
    sentences: list[Claim] = Field(min_length=1, max_length=60)


class Draft(StrictModel):
    id: Id = Field(default_factory=new_id)
    listing_id: Id
    version: int = Field(ge=1)
    content: CopywriterOutput
    model: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def title(self) -> str:
        return self.content.title.text

    @property
    def description(self) -> str:
        return " ".join(claim.text for claim in self.content.sentences)

    @property
    def all_claims(self) -> list[Claim]:
        return [self.content.title, *self.content.sentences]

    @property
    def referenced_fact_ids(self) -> set[str]:
        return {fact_id for claim in self.all_claims for fact_id in claim.fact_ids}


class IssueType(StrEnum):
    UNSUPPORTED_CLAIM = "unsupported_claim"
    MISLEADING_STATEMENT = "misleading_statement"
    SENSITIVE_INFO = "sensitive_info"
    PROHIBITED_PHRASE = "prohibited_phrase"
    DISCRIMINATORY_LANGUAGE = "discriminatory_language"
    INCONSISTENCY = "inconsistency"
    PROMPT_INJECTION = "prompt_injection"


class IssueSeverity(StrEnum):
    WARN = "warn"
    BLOCK = "block"


class SafetyDecision(StrEnum):
    PASS = "pass"  # noqa: S105 -- a verdict value, not a password
    WARN = "warn"
    BLOCK = "block"


class ReviewerType(StrEnum):
    CODE = "code"
    LLM = "llm"


class SafetyIssue(StrictModel):
    issue_type: IssueType
    severity: IssueSeverity
    message: str = Field(min_length=1, max_length=500)
    # Index into Draft.all_claims (0 = title); None when the issue is not tied to one claim.
    claim_index: int | None = Field(default=None, ge=0)
    # Fixable issues send the draft back to the Copywriter (max 2 rounds); others block it.
    fixable: bool = True


class SafetyVerdict(StrictModel):
    draft_id: Id
    reviewer_type: ReviewerType
    issues: list[SafetyIssue] = Field(default_factory=list)

    @property
    def decision(self) -> SafetyDecision:
        """Derived from the issues by code, so no reviewer can say "pass" next to a blocker."""
        severities = {issue.severity for issue in self.issues}
        if IssueSeverity.BLOCK in severities:
            return SafetyDecision.BLOCK
        if IssueSeverity.WARN in severities:
            return SafetyDecision.WARN
        return SafetyDecision.PASS


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


ActionName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]
# Scalars only: audit details are short, already-masked summaries, never raw documents.
AuditDetailValue = str | int | float | bool | None


class AuditEntry(StrictModel):
    """Who did what, to which resource, and when (architecture doc §7.7)."""

    id: Id = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    actor_type: ActorType
    actor_name: str = Field(min_length=1, max_length=64)
    action: ActionName
    resource_type: ActionName
    resource_id: Id | None = None
    details: dict[str, AuditDetailValue] = Field(default_factory=dict, max_length=50)


class QualityWarning(StrEnum):
    LOW_RESOLUTION = "low_resolution"
    BLURRY = "blurry"
    TOO_DARK = "too_dark"
    OVEREXPOSED = "overexposed"


class PrivacyFlag(StrEnum):
    """Set by the Vision Analyst; blurring is left to the user."""

    LICENSE_PLATE = "license_plate"
    FACE = "face"
    DOOR_NUMBER = "door_number"
    DOCUMENT = "document"
    SCREEN_PERSONAL_INFO = "screen_personal_info"
    # Set by the Photo Curator: the vision call failed, so nobody checked this photo.
    NOT_ANALYZED = "not_analyzed"


Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PerceptualHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
# "<listing_id>/<photo_id>.jpg": never derived from a user-supplied file name.
StorageKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}/[0-9a-f]{32}\.jpg$")]


class ListingPhoto(StrictModel):
    id: Id = Field(default_factory=new_id)
    listing_id: Id
    storage_key: StorageKey
    sha256: Sha256Hex
    perceptual_hash: PerceptualHash
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    blur_score: float = Field(ge=0)
    brightness: float = Field(ge=0, le=255)
    quality_warnings: list[QualityWarning] = Field(default_factory=list)
    privacy_flags: list[PrivacyFlag] = Field(default_factory=list)
    order_index: int = Field(ge=0)
    is_cover: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check_storage_key(self) -> "ListingPhoto":
        if self.storage_key != f"{self.listing_id}/{self.id}.jpg":
            raise ValueError("storage_key must be '<listing_id>/<photo_id>.jpg'")
        return self


# --- Agents, runs and tool calls --------------------------------------------------------


class AgentName(StrEnum):
    INTAKE_GUARD = "intake_guard"
    PHOTO_CURATOR = "photo_curator"
    VISION_ANALYST = "vision_analyst"
    FACT_RECONCILER = "fact_reconciler"
    GAP_DETECTOR = "gap_detector"
    MARKET_ANALYST = "market_analyst"
    COPYWRITER = "copywriter"
    SAFETY_REVIEWER = "safety_reviewer"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolCallRecord(StrictModel):
    tool_name: str = Field(min_length=1, max_length=64)
    allowed: bool
    # Types and sizes only, never argument values (they may contain personal data).
    args_summary: str = Field(max_length=500)
    error: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)


class AgentRunRecord(StrictModel):
    id: Id
    listing_id: Id
    agent_name: AgentName
    status: RunStatus
    model: str | None = None
    prompt_version: str | None = None
    llm_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=200)
    started_at: datetime
    finished_at: datetime


# --- Human interaction: clarifications and approvals ------------------------------------


class ClarificationStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    DECLINED = "declined"


class Clarification(StrictModel):
    id: Id = Field(default_factory=new_id)
    listing_id: Id
    field_key: FieldKey
    question: str = Field(min_length=1, max_length=300)
    status: ClarificationStatus = ClarificationStatus.OPEN
    answer_fact_id: Id | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalGate(StrEnum):
    FACTS = "facts"
    FINAL = "final"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class Approval(StrictModel):
    """Only ever created by a human action through the workflow, never by an agent."""

    id: Id = Field(default_factory=new_id)
    listing_id: Id
    gate: ApprovalGate
    decision: ApprovalDecision
    # A final approval covers one exact draft version, never "the listing" in general.
    draft_id: Id | None = None
    comment: str | None = Field(default=None, max_length=1000)
    actor_name: str = Field(min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _final_needs_draft(self) -> "Approval":
        if (self.gate is ApprovalGate.FINAL) != (self.draft_id is not None):
            raise ValueError("final approvals, and only final approvals, reference a draft")
        return self


class ListingInput(StrictModel):
    """What the seller types into the form. Values are raw strings, validated later."""

    category: Category = Category.CAR
    fields: dict[FieldKey, str] = Field(default_factory=dict, max_length=50)
    notes: str = Field(default="", max_length=2000)
    # Phone numbers / e-mail addresses may be added knowingly (architecture doc §7.5).
    confirm_contact_info: bool = False


class MarketStatus(StrEnum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    MISSING_FACTS = "missing_facts"


class MarketSummary(StrictModel):
    """Informational only. Always computed from synthetic data, never a price recommendation."""

    status: MarketStatus
    sample_size: int = Field(default=0, ge=0)
    median_price_try: int | None = None
    q1_price_try: int | None = None
    q3_price_try: int | None = None
    widening_notes: list[str] = Field(default_factory=list)
    is_synthetic: bool = True
