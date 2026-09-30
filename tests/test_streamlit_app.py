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
from listing_assistant.workflow import ListingWorkflow, WorkflowError
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


def test_intake_warnings_are_shown_after_creating_a_listing(app_env):
    app = open_app()
    app.text_input(key="make").input("Renault")
    app.text_input(key="reason_for_selling").input("Arayın 0532 123 45 67")
    [confirm] = [c for c in app.checkbox if "İletişim bilgisi" in c.label]
    confirm.check()
    button(app, "İlanı oluştur").click().run()
    assert not app.exception
    assert any("kişisel veri" in w.value and "telefon" in w.value for w in app.warning)
    assert not any("0532 123 45 67" in w.value for w in app.warning)


def facts_review_listing(llm=None) -> str:
    with workflow_for_app() as workflow:
        if llm is not None:
            workflow._llm = llm
        listing = workflow.create_listing(ListingInput(fields=FORM)).listing
        for seed in range(2):
            workflow.add_photo(listing.id, encode(synthetic_scene(seed=seed)))
        workflow.run_analysis(listing.id)
    return listing.id


def test_seller_fact_with_contact_details_needs_the_checkbox(app_env):
    listing_id = facts_review_listing()
    app = open_app(listing_id)
    [field] = [s for s in app.selectbox if s.label == "Alan"]
    field.set_value("reason_for_selling").run()
    app.text_input(key="manual-reason_for_selling").input("Arayın 0532 123 45 67")
    [b for b in app.button if b.key == "manual-save"][0].click().run()
    assert any("onay kutusunu" in e.value for e in app.error)

    app.checkbox(key="manual-confirm").check()
    [b for b in app.button if b.key == "manual-save"][0].click().run()
    assert not app.exception and not app.error
    with workflow_for_app() as workflow:
        keys = {f.field_key for f in workflow.approved_facts(listing_id)}
    assert "reason_for_selling" in keys


def test_correcting_a_fact_passes_the_contact_confirmation(app_env):
    listing_id = facts_review_listing()
    with workflow_for_app() as workflow:
        [fact] = [f for f in workflow.approved_facts(listing_id) if f.field_key == "color"]
    app = open_app(listing_id)
    app.text_input(key=f"fix-{fact.id}").input("beyaz, bilgi: satici.test@example.com")
    app.checkbox(key=f"fix-confirm-{fact.id}").check()
    [b for b in app.button if b.key == f"correct-{fact.id}"][0].click().run()
    assert not app.exception and not app.error
    with workflow_for_app() as workflow:
        [color] = [f for f in workflow.approved_facts(listing_id) if f.field_key == "color"]
    assert "example.com" in color.value


def test_listing_name_in_the_title_is_escaped(app_env):
    """Negative test: Markdown typed by the seller is shown as text in the page title."""
    with workflow_for_app() as workflow:
        fields = {**FORM, "make": "[x](https://example.test)"}
        listing_id = workflow.create_listing(ListingInput(fields=fields)).listing.id
    app = open_app(listing_id)
    assert not app.exception
    assert app.title[0].value.startswith(r"\[x\]\(https")


def test_unanalysed_photo_is_labelled_in_turkish(app_env):
    from listing_assistant.llm import LLMOutputError
    from scenario import photo_analysis

    calls = []

    def first_fails(request):
        calls.append(request)
        return LLMOutputError("unusable") if len(calls) == 1 else photo_analysis(request)

    listing_id = facts_review_listing(team(PhotoAnalysis=first_fails))
    app = open_app(listing_id)
    assert not app.exception
    assert any("İncelenemedi" in c.value for c in app.caption)


def test_export_failure_in_the_exported_state_is_shown_not_raised(app_env, monkeypatch):
    from scenario import through_review

    with workflow_for_app() as workflow:
        listing_id = workflow.create_listing(ListingInput(fields=FORM)).listing.id
        workflow.add_photo(listing_id, encode(synthetic_scene(seed=3)))
        through_review(workflow, listing_id)
        draft = workflow.run_generation(listing_id).draft
        workflow.approve_final(listing_id, draft.version)
        workflow.export_listing(listing_id)
    assert status_of(listing_id) is S.EXPORTED

    def refuse(self, listing_id):
        raise WorkflowError("Dışa aktarılamadı.")

    monkeypatch.setattr(ListingWorkflow, "export_listing", refuse)
    app = open_app(listing_id)
    assert not app.exception
    assert any("Dışa aktarılamadı" in e.value for e in app.error)
