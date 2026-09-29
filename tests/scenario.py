"""A scripted, well-behaved "agent team" for workflow tests, built on FakeLLMClient.

Each handler can be replaced per test to simulate a manipulated or buggy model.
"""

import html
import json
from collections.abc import Callable

from fakes import FakeLLMClient, by_output_model, request_text
from listing_assistant.agent_io import (
    GapQuestion,
    GapQuestions,
    NoteExtraction,
    PhotoAnalysis,
    PhotoView,
    SafetyFindings,
    VisionObservation,
)
from listing_assistant.llm import LLMRequest
from listing_assistant.models import (
    Claim,
    CopywriterOutput,
    FactStatus,
    ListingInput,
    ListingStatus,
)
from synthetic_images import encode, synthetic_scene


def approved_facts_in(request: LLMRequest) -> list[dict]:
    text = request_text(request)
    payload = text.split("<approved_facts>")[1].split("</approved_facts>")[0]
    return json.loads(html.unescape(payload))


def honest_copy(request: LLMRequest) -> CopywriterOutput:
    """One plain sentence per approved fact, each citing exactly that fact."""
    facts = approved_facts_in(request)
    by_field = {f["field"]: f for f in facts}
    title_facts = [by_field[k] for k in ("make", "model") if k in by_field] or facts[:1]
    return CopywriterOutput(
        title=Claim(
            text=" ".join(f["value"] for f in title_facts),
            fact_ids=[f["fact_id"] for f in title_facts],
        ),
        sentences=[
            Claim(text=f"{f['label']}: {f['value']}.", fact_ids=[f["fact_id"]]) for f in facts
        ],
    )


def photo_analysis(request: LLMRequest) -> PhotoAnalysis:
    return PhotoAnalysis(
        view=PhotoView.EXTERIOR_FRONT,
        observations=[
            VisionObservation(field_key="color", value="kırmızı", confidence=0.65),
            VisionObservation(field_key="body_type", value="hatchback", confidence=0.9),
        ],
    )


def gap_questions(request: LLMRequest) -> GapQuestions:
    keys = [
        line.split("- ", 1)[1].split(" ", 1)[0]
        for line in request_text(request).splitlines()
        if line.startswith("- ")
    ]
    return GapQuestions(
        questions=[GapQuestion(field_key=k, question_tr=f"{k} nedir?") for k in keys]
    )


def team(**overrides: Callable[[LLMRequest], object]) -> FakeLLMClient:
    handlers = {
        "PhotoAnalysis": photo_analysis,
        "NoteExtraction": lambda r: NoteExtraction(),
        "GapQuestions": gap_questions,
        "CopywriterOutput": honest_copy,
        "SafetyFindings": lambda r: SafetyFindings(),
    }
    handlers.update(overrides)
    return FakeLLMClient(by_output_model(**handlers))


# --- workflow helpers shared by the workflow, approval and red-team tests ---

FORM = {"make": "Renault", "model": "Clio", "color": "beyaz"}


def new_listing(workflow, fields=FORM, notes="", photos=1):
    result = workflow.create_listing(ListingInput(fields=fields, notes=notes))
    for seed in range(photos):
        workflow.add_photo(result.listing.id, encode(synthetic_scene(seed=seed)))
    return result.listing.id


def approve_all(workflow, listing_id):
    for fact in workflow.facts_overview(listing_id).facts:
        if fact.status is FactStatus.PROPOSED:
            workflow.approve_fact(listing_id, fact.id)


def answer_all(workflow, listing_id, answers):
    status = ListingStatus.NEEDS_INFO
    for question in workflow.open_questions(listing_id):
        status = workflow.answer_question(listing_id, question.id, answers[question.field_key])
    return status


def through_review(workflow, listing_id, answers=None):
    workflow.run_analysis(listing_id)
    approve_all(workflow, listing_id)
    status = workflow.complete_fact_review(listing_id)
    if status is ListingStatus.NEEDS_INFO:
        status = answer_all(
            workflow, listing_id, answers or {"model_year": "2019", "mileage_km": "85.000"}
        )
    return status


def copy_with_extra_sentence(text):
    def write(request):
        honest = honest_copy(request)
        extra = Claim(text=text, fact_ids=[honest.title.fact_ids[0]])
        return CopywriterOutput(title=honest.title, sentences=[*honest.sentences, extra])

    return write
