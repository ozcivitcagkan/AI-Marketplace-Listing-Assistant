import pytest
from pydantic import ValidationError

from listing_assistant.models import (
    Claim,
    CopywriterOutput,
    Draft,
    Fact,
    FactSource,
    IssueSeverity,
    IssueType,
    Listing,
    ReviewerType,
    SafetyDecision,
    SafetyIssue,
    SafetyVerdict,
    VisionFactProposal,
    new_id,
)

LISTING_ID = new_id()
PHOTO_ID = new_id()


def make_proposal(**overrides):
    data = {
        "field_key": "color",
        "value": "kırmızı",
        "confidence": 0.7,
        "evidence_photo_id": PHOTO_ID,
    }
    return VisionFactProposal(**(data | overrides))


def make_draft(sentences: list[Claim]) -> Draft:
    return Draft(
        listing_id=LISTING_ID,
        version=1,
        content=CopywriterOutput(
            title=Claim(text="Kırmızı sedan", fact_ids=[new_id()]), sentences=sentences
        ),
        model="fake-model",
        prompt_version="copywriter-v1",
    )


# --- Security: model output cannot smuggle in extra authority ---------------------------


def test_vision_proposal_cannot_claim_user_source():
    with pytest.raises(ValidationError, match="extra"):
        make_proposal(source="user")


def test_vision_proposal_cannot_mark_itself_approved():
    with pytest.raises(ValidationError, match="extra"):
        make_proposal(status="approved")


def test_copywriter_cannot_return_free_text_description():
    with pytest.raises(ValidationError, match="extra"):
        CopywriterOutput.model_validate(
            {
                "title": {"text": "Başlık", "fact_ids": [new_id()]},
                "sentences": [{"text": "Cümle.", "fact_ids": [new_id()]}],
                "description": "Hatasız, boyasız, tramersiz!",
            }
        )


@pytest.mark.parametrize("bad_id", ["../other-listing", "1", "X" * 32, ""])
def test_malformed_ids_are_rejected(bad_id):
    with pytest.raises(ValidationError):
        make_proposal(evidence_photo_id=bad_id)


@pytest.mark.parametrize("confidence", [-0.1, 1.01])
def test_confidence_must_be_between_zero_and_one(confidence):
    with pytest.raises(ValidationError):
        make_proposal(confidence=confidence)


def test_models_are_immutable():
    proposal = make_proposal()
    with pytest.raises(ValidationError):
        proposal.value = "mavi"


# --- Facts ------------------------------------------------------------------------------


def test_vision_fact_requires_evidence_photo():
    with pytest.raises(ValidationError, match="evidence_photo_id"):
        Fact(
            listing_id=LISTING_ID,
            field_key="color",
            value="kırmızı",
            source=FactSource.VISION,
            confidence=0.6,
        )


def test_user_fact_cannot_reference_evidence_photo():
    with pytest.raises(ValidationError, match="only vision facts"):
        Fact(
            listing_id=LISTING_ID,
            field_key="color",
            value="kırmızı",
            source=FactSource.USER,
            evidence_photo_id=PHOTO_ID,
        )


def test_valid_user_fact():
    fact = Fact(
        listing_id=LISTING_ID, field_key="mileage_km", value="120000", source=FactSource.USER
    )
    assert fact.status == "proposed"
    assert len(fact.id) == 32


# --- Claims and drafts ------------------------------------------------------------------


def test_claim_without_source_fact_is_rejected():
    with pytest.raises(ValidationError, match="fact_ids"):
        Claim(text="Araç kazasızdır.", fact_ids=[])


def test_draft_description_is_built_from_sourced_sentences():
    fact_a, fact_b = new_id(), new_id()
    draft = make_draft(
        [
            Claim(text="Araç kırmızıdır.", fact_ids=[fact_a]),
            Claim(text="Otomatik vitestir.", fact_ids=[fact_a, fact_b]),
        ]
    )
    assert draft.title == "Kırmızı sedan"
    assert draft.description == "Araç kırmızıdır. Otomatik vitestir."
    assert {fact_a, fact_b} <= draft.referenced_fact_ids
    assert len(draft.all_claims) == 3


def test_draft_json_round_trip():
    draft = make_draft([Claim(text="Araç kırmızıdır.", fact_ids=[new_id()])])
    assert Draft.model_validate_json(draft.model_dump_json()) == draft


def test_new_listing_starts_as_draft():
    assert Listing().status == "draft"


# --- Safety verdict ---------------------------------------------------------------------


def issue(severity: IssueSeverity) -> SafetyIssue:
    return SafetyIssue(issue_type=IssueType.PROHIBITED_PHRASE, severity=severity, message="x")


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        ([], SafetyDecision.PASS),
        ([IssueSeverity.WARN], SafetyDecision.WARN),
        ([IssueSeverity.WARN, IssueSeverity.BLOCK], SafetyDecision.BLOCK),
    ],
)
def test_safety_decision_is_derived_from_issues(severities, expected):
    verdict = SafetyVerdict(
        draft_id=new_id(), reviewer_type=ReviewerType.CODE, issues=[issue(s) for s in severities]
    )
    assert verdict.decision is expected


def test_reviewer_cannot_supply_its_own_decision():
    with pytest.raises(ValidationError, match="extra"):
        SafetyVerdict.model_validate(
            {
                "draft_id": new_id(),
                "reviewer_type": "llm",
                "decision": "pass",
                "issues": [
                    {"issue_type": "sensitive_info", "severity": "block", "message": "IBAN"}
                ],
            }
        )
