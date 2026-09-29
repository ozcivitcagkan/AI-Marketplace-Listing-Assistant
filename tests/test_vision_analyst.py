import pytest

from fakes import FakeLLMClient, has_image, request_text
from listing_assistant.agent_io import PhotoAnalysis, PhotoView, VisionObservation
from listing_assistant.agents.vision_analyst import run_vision_analyst, screen_analysis
from listing_assistant.llm import BudgetExceededError, LlmBudget, LLMOutputError
from listing_assistant.models import AgentName, PrivacyFlag, new_id
from listing_assistant.photo_intake import ingest_photo
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import ListingTools
from synthetic_images import encode, synthetic_scene


def obs(key, value, confidence=0.8):
    return VisionObservation(field_key=key, value=value, confidence=confidence)


def make_run(conn, settings, schema, listing, llm, budget=None, agent=AgentName.VISION_ANALYST):
    tools = ListingTools(conn, settings, schema, listing.id).as_mapping()
    return AgentRun(agent, listing.id, tools, llm, budget or LlmBudget(40, 0))


def add_photos(conn, settings, listing, count):
    return [
        ingest_photo(conn, settings, listing.id, encode(synthetic_scene(seed=i)))
        for i in range(count)
    ]


# --- screen_analysis: pure validation of model output ------------------------------------


def test_unobservable_unknown_and_invalid_observations_are_dropped(schema):
    photo_id = new_id()
    analysis = PhotoAnalysis(
        view=PhotoView.EXTERIOR_FRONT,
        observations=[
            obs("body_type", "SUV"),
            obs("accident_history", "kazasız", 0.99),  # never observable
            obs("model_year", "2018"),  # never observable
            obs("engine_secret", "x"),  # not a field at all
            obs("mileage_km", "about 120k"),  # malformed number
        ],
    )
    result = screen_analysis(photo_id, analysis, schema)

    assert [(p.field_key, p.value) for p in result.proposals] == [("body_type", "suv")]
    assert result.proposals[0].evidence_photo_id == photo_id
    reasons = {r.field_key: r.reason for r in result.rejected}
    assert reasons == {
        "accident_history": "not observable",
        "model_year": "not observable",
        "engine_secret": "unknown field",
        "mileage_km": "invalid value",
    }


def test_most_confident_value_wins_within_one_photo(schema):
    analysis = PhotoAnalysis(
        view=PhotoView.EXTERIOR_SIDE,
        observations=[obs("color", "kırmızı", 0.4), obs("color", "bordo", 0.6)],
    )
    [proposal] = screen_analysis(new_id(), analysis, schema).proposals
    assert proposal.value == "bordo"


# --- run_vision_analyst: through the tool gate with a fake model -------------------------


def test_each_photo_is_analysed_with_its_own_evidence(conn, settings, schema, listing):
    photos = add_photos(conn, settings, listing, 3)
    fake = FakeLLMClient(
        lambda r: PhotoAnalysis(
            view=PhotoView.EXTERIOR_FRONT,
            observations=[obs("color", "kırmızı", 0.6)],
            privacy_flags=[PrivacyFlag.LICENSE_PLATE],
        )
    )
    run = make_run(conn, settings, schema, listing, fake)
    report = run_vision_analyst(run, schema)

    assert {p.evidence_photo_id for p in report.proposals} == {p.id for p in photos}
    assert all(r.privacy_flags == (PrivacyFlag.LICENSE_PLATE,) for r in report.photos)
    assert run.llm_calls == 3
    # The model saw an image plus the trusted field list, and only observable fields.
    request = fake.requests[0]
    assert has_image(request)
    assert "- color (text)" in request_text(request)
    assert "accident_history" not in request_text(request)


def test_one_bad_answer_does_not_sink_the_whole_analysis(conn, settings, schema, listing):
    add_photos(conn, settings, listing, 2)
    answers = iter([LLMOutputError("refused"), PhotoAnalysis(view=PhotoView.DASHBOARD)])
    run = make_run(conn, settings, schema, listing, FakeLLMClient(lambda r: next(answers)))
    report = run_vision_analyst(run, schema)
    assert sorted(r.analyzed for r in report.photos) == [False, True]


def test_all_answers_bad_fails_the_run(conn, settings, schema, listing):
    add_photos(conn, settings, listing, 2)
    run = make_run(conn, settings, schema, listing, FakeLLMClient(lambda r: {"view": "x"}))
    with pytest.raises(LLMOutputError):
        run_vision_analyst(run, schema)
    assert run.to_record().status == "failed"


def test_budget_is_enforced(conn, settings, schema, listing):
    add_photos(conn, settings, listing, 3)
    fake = FakeLLMClient(lambda r: PhotoAnalysis(view=PhotoView.OTHER))
    run = make_run(conn, settings, schema, listing, fake, budget=LlmBudget(limit=2, used=0))
    with pytest.raises(BudgetExceededError):
        run_vision_analyst(run, schema)
    assert len(fake.requests) == 2


def test_instruction_text_in_photo_is_reported(conn, settings, schema, listing):
    add_photos(conn, settings, listing, 1)
    fake = FakeLLMClient(
        lambda r: PhotoAnalysis(view=PhotoView.OTHER, instruction_text_detected=True)
    )
    report = run_vision_analyst(make_run(conn, settings, schema, listing, fake), schema)
    assert report.photos[0].instruction_text_detected


def test_copywriter_cannot_use_vision_tools(conn, settings, schema, listing):
    add_photos(conn, settings, listing, 1)
    run = make_run(
        conn, settings, schema, listing, FakeLLMClient(lambda r: None), agent=AgentName.COPYWRITER
    )
    with pytest.raises(PermissionError):
        run_vision_analyst(run, schema)
    assert run.tool_calls[0].allowed is False
