import sqlite3

import pytest

from fakes import FakeLLMClient, request_text
from listing_assistant.agent_io import GapQuestion, GapQuestions
from listing_assistant.agents.gap_detector import find_gaps, run_gap_detector
from listing_assistant.db.repositories import ClarificationRepository, FactRepository
from listing_assistant.llm import LlmBudget, LLMOutputError
from listing_assistant.models import (
    AgentName,
    ClarificationStatus,
    Fact,
    FactSource,
    FactStatus,
)
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import ListingTools


def approved(listing_id, key, value):
    return Fact(
        listing_id=listing_id,
        field_key=key,
        value=value,
        source=FactSource.USER,
        status=FactStatus.APPROVED,
    )


def make_run(conn, settings, schema, listing, llm, agent=AgentName.GAP_DETECTOR):
    tools = ListingTools(conn, settings, schema, listing.id).as_mapping()
    return AgentRun(agent, listing.id, tools, llm, LlmBudget(40, 0))


def test_missing_required_fields_are_found_by_code(schema, listing):
    facts = [approved(listing.id, "make", "Renault"), approved(listing.id, "color", "beyaz")]
    gaps = find_gaps(schema, facts, settled_keys={"model_year"})
    assert [g.key for g in gaps] == ["model", "mileage_km"]


def test_questions_are_saved_and_unknown_keys_ignored(conn, settings, schema, listing):
    FactRepository(conn).add(approved(listing.id, "make", "Renault"))
    fake = FakeLLMClient(
        lambda r: GapQuestions(
            questions=[
                GapQuestion(field_key="model", question_tr="Aracınızın modeli nedir?"),
                GapQuestion(field_key="accident_history", question_tr="Kaza yaptı mı?"),
            ]
        )
    )
    created = run_gap_detector(make_run(conn, settings, schema, listing, fake), schema, set())

    assert {c.field_key: c.question for c in created} == {
        "model": "Aracınızın modeli nedir?",
        "model_year": "Lütfen aracınızın model yılı bilgisini girer misiniz?",
        "mileage_km": "Lütfen aracınızın kilometre bilgisini girer misiniz?",
    }
    # The model only saw field names from the schema, never seller data.
    assert "Renault" not in request_text(fake.requests[0])


def test_model_failure_falls_back_to_templates(conn, settings, schema, listing):
    fake = FakeLLMClient(lambda r: LLMOutputError("refused"))
    created = run_gap_detector(make_run(conn, settings, schema, listing, fake), schema, set())
    assert len(created) == 4
    assert all(c.question.startswith("Lütfen") for c in created)


def test_no_gaps_means_no_model_call(conn, settings, schema, listing):
    repo = FactRepository(conn)
    for key, value in [
        ("make", "Fiat"),
        ("model", "Egea"),
        ("model_year", "2020"),
        ("mileage_km", "50000"),
    ]:
        repo.add(approved(listing.id, key, value))
    fake = FakeLLMClient(lambda r: AssertionError("no call expected"))
    assert run_gap_detector(make_run(conn, settings, schema, listing, fake), schema, set()) == []
    assert fake.requests == []


def test_clarification_closes_only_once(conn, settings, schema, listing):
    fake = FakeLLMClient(lambda r: GapQuestions())
    [first, *_] = run_gap_detector(make_run(conn, settings, schema, listing, fake), schema, set())
    repo = ClarificationRepository(conn)
    repo.close(listing.id, first.id, ClarificationStatus.DECLINED)
    with pytest.raises(LookupError):
        repo.close(listing.id, first.id, ClarificationStatus.DECLINED)
    with pytest.raises(sqlite3.IntegrityError, match="open clarification"), conn:
        conn.execute("UPDATE clarifications SET status = 'open' WHERE id = ?", (first.id,))


def test_only_the_gap_detector_may_ask_questions(conn, settings, schema, listing):
    run = make_run(
        conn, settings, schema, listing, FakeLLMClient(lambda r: None), agent=AgentName.COPYWRITER
    )
    with pytest.raises(PermissionError):
        run.call("request_user_input", questions={"model": "?"})
