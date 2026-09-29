"""Output contracts for LLM calls: the ONLY shapes a model may return.

These are deliberately narrower than the internal models. A model never supplies IDs
it cannot know (listing, photo), a fact source, a status, or a final decision: code
adds or derives those. Anything that does not fit is discarded (architecture doc §7.2).
"""

from enum import StrEnum

from pydantic import Field

from listing_assistant.models import (
    Confidence,
    IssueSeverity,
    IssueType,
    PrivacyFlag,
    StrictModel,
)


class PhotoView(StrEnum):
    EXTERIOR_FRONT = "exterior_front"
    EXTERIOR_SIDE = "exterior_side"
    EXTERIOR_REAR = "exterior_rear"
    INTERIOR_FRONT = "interior_front"
    INTERIOR_REAR = "interior_rear"
    DASHBOARD = "dashboard"
    TRUNK = "trunk"
    WHEEL = "wheel"
    ENGINE_BAY = "engine_bay"
    OTHER = "other"


class VisionObservation(StrictModel):
    field_key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=200)
    confidence: Confidence


class PhotoAnalysis(StrictModel):
    """Vision Analyst output for ONE photo. The photo ID is added by code, not the model."""

    view: PhotoView
    observations: list[VisionObservation] = Field(default_factory=list, max_length=20)
    privacy_flags: list[PrivacyFlag] = Field(default_factory=list, max_length=5)
    # Text in the photo that looks like instructions (e.g. a note saying "write accident-free").
    instruction_text_detected: bool = False


class NoteProposal(StrictModel):
    field_key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=200)


class NoteExtraction(StrictModel):
    proposals: list[NoteProposal] = Field(default_factory=list, max_length=20)


class GapQuestion(StrictModel):
    field_key: str = Field(min_length=1, max_length=64)
    question_tr: str = Field(min_length=1, max_length=300)


class GapQuestions(StrictModel):
    questions: list[GapQuestion] = Field(default_factory=list, max_length=20)


class SafetyFinding(StrictModel):
    issue_type: IssueType
    severity: IssueSeverity
    message: str = Field(min_length=1, max_length=300)
    claim_index: int | None = Field(default=None, ge=0)


class SafetyFindings(StrictModel):
    findings: list[SafetyFinding] = Field(default_factory=list, max_length=30)
