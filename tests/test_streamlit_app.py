"""Drive the real Streamlit script headlessly (streamlit.testing.AppTest) with a fake model."""

from contextlib import contextmanager
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from listing_assistant import llm as llm_module
from listing_assistant.config import load_settings
from listing_assistant.db import open_database
from listing_assistant.models import ListingInput, ListingStatus
from listing_assistant.workflow import ListingWorkflow
from scenario import FORM, team
from synthetic_images import encode, synthetic_scene

APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")
S = ListingStatus


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LA_DATA_DIR", str(tmp_path / "data"))
    fake = team()
    monkeypatch.setattr(llm_module, "AnthropicLLMClient", lambda model: fake)
    st.cache_resource.clear()
    yield fake
    st.cache_resource.clear()


def open_app(listing_id=None) -> AppTest:
    app = AppTest.from_file(APP, default_timeout=60)
    if listing_id:
        app.session_state["listing_id"] = listing_id
    return app.run()


def button(app: AppTest, label: str):
    matches = [b for b in app.button if b.label == label]
    assert matches, f"no button {label!r}; have {[b.label for b in app.button]}"
    return matches[0]


@contextmanager
def workflow_for_app():
    settings = load_settings()
    conn = open_database(settings.db_path)
    try:
        yield ListingWorkflow(conn, settings, team())
    finally:
        conn.close()


def listing_with_photos(photos=2) -> str:
    with workflow_for_app() as workflow:
        listing = workflow.create_listing(ListingInput(fields=FORM)).listing
        for seed in range(photos):
            workflow.add_photo(listing.id, encode(synthetic_scene(seed=seed)))
    return listing.id


def status_of(listing_id):
    with workflow_for_app() as workflow:
        return workflow.get_listing(listing_id).status


def test_start_page_renders(app_env):
    app = open_app()
    assert not app.exception
    assert app.title[0].value == "Yeni araç ilanı"


def test_create_listing_through_the_form(app_env):
    app = open_app()
    app.text_input(key="make").input("Renault")
    app.text_input(key="model").input("Clio")
    button(app, "İlanı oluştur").click().run()
    assert not app.exception
    listing_id = app.session_state["listing_id"]
    assert status_of(listing_id) is S.DRAFT


def test_refusals_are_shown_not_raised(app_env):
    listing_id = listing_with_photos(photos=0)
    app = open_app(listing_id)
    button(app, "Fotoğrafları analiz et").click().run()
    assert not app.exception
    assert "en az bir fotoğraf" in app.error[0].value


def test_full_journey_through_the_ui(app_env):
    listing_id = listing_with_photos()
    app = open_app(listing_id)

    button(app, "Fotoğrafları analiz et").click().run()
    assert status_of(listing_id) is S.FACTS_REVIEW
    while approve := [b for b in app.button if b.label == "Onayla"]:
        approve[0].click().run()
    button(app, "Bilgiler doğru, devam et").click().run()
    assert status_of(listing_id) is S.NEEDS_INFO

    answers = {"model_year": "2019", "mileage_km": "85000"}
    with workflow_for_app() as workflow:
        questions = workflow.open_questions(listing_id)
    for question in questions:
        app.text_input(key=f"answer-{question.id}").input(answers[question.field_key])
        [b for b in app.button if b.key == f"send-{question.id}"][0].click().run()
    assert status_of(listing_id) is S.GENERATING

    button(app, "İlan metnini yaz").click().run()
    assert status_of(listing_id) is S.READY_FOR_APPROVAL
    button(app, "Bu sürümü onayla").click().run()
    assert status_of(listing_id) is S.APPROVED

    button(app, "İlanı dışa aktar").click().run()
    assert not app.exception
    assert "Renault Clio" in app.code[0].value
    assert status_of(listing_id) is S.EXPORTED


def test_form_fields_run_left_to_right_for_tab_order(app_env):
    """Each row is rendered left then right, so Tab goes Marka -> Model -> Motor -> Paket."""
    app = open_app()
    keys = [widget.key for widget in app.text_input][:6]
    assert keys == ["make", "model", "engine", "trim_package", "model_year", "mileage_km"]


def test_seller_text_is_escaped_in_the_listing_preview(app_env):
    """Negative test: HTML typed by the seller is shown as text, never rendered as markup."""
    with workflow_for_app() as workflow:
        fields = {**FORM, "city": "<img src=x onerror=alert(1)>"}
        listing_id = workflow.create_listing(ListingInput(fields=fields)).listing.id
        workflow.add_photo(listing_id, encode(synthetic_scene(seed=1)))
    app = open_app(listing_id)
    button(app, "Fotoğrafları analiz et").click().run()
    while approve := [b for b in app.button if b.label == "Onayla"]:
        approve[0].click().run()
    button(app, "Bilgiler doğru, devam et").click().run()
    with workflow_for_app() as workflow:
        for question in workflow.open_questions(listing_id):
            answer = {"model_year": "2019", "mileage_km": "85000"}[question.field_key]
            workflow.answer_question(listing_id, question.id, answer)
    app = open_app(listing_id)
    button(app, "İlan metnini yaz").click().run()
    assert status_of(listing_id) is S.READY_FOR_APPROVAL
    html_blocks = [m.value for m in app.markdown if 'class="la-card"' in m.value]
    assert html_blocks and "<img" not in html_blocks[0]
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_blocks[0]


def test_blocked_listing_offers_a_way_back_to_the_facts(app_env, monkeypatch):
    from scenario import copy_with_extra_sentence

    blocking = team(CopywriterOutput=copy_with_extra_sentence("Aracım kazasız."))
    with workflow_for_app() as workflow:
        workflow._llm = blocking
        listing_id = workflow.create_listing(ListingInput(fields=FORM)).listing.id
        workflow.add_photo(listing_id, encode(synthetic_scene(seed=2)))
        from scenario import through_review

        through_review(workflow, listing_id)
        assert workflow.run_generation(listing_id).status is S.BLOCKED
    app = open_app(listing_id)
    assert not app.exception
    assert any("kesin bir ifade" in m.value for m in app.markdown)
    button(app, "Bilgilere dön").click().run()
    assert status_of(listing_id) is S.FACTS_REVIEW
