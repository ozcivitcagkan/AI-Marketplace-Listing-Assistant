"""Streamlit UI: a thin, replaceable view over `ListingWorkflow`.

No rule lives here. Every button calls one workflow method, and the workflow checks
status, scope, permissions and approvals itself (architecture doc §3: the security
boundary is the backend, never the frontend). Everything the seller reads is Turkish.

Content that comes from the seller or a model is escaped before it is rendered, both in
Markdown (`md`) and in the few HTML blocks (`html.escape`), so it can never inject markup.

Run with:

    streamlit run app/streamlit_app.py
"""

import html
import os
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

import streamlit as st

from listing_assistant.agents.safety_reviewer import combined_decision, seller_message
from listing_assistant.config import load_settings
from listing_assistant.db import open_database
from listing_assistant.field_schema import FieldDefinition, FieldGroup, ValueType
from listing_assistant.listing_format import EQUIPMENT_HEADING, SECTION_HEADINGS, SPEC_HEADING
from listing_assistant.llm import AnthropicLLMClient, LLMError
from listing_assistant.models import (
    STATUS_LABELS_TR,
    FactSource,
    FactStatus,
    IssueSeverity,
    IssueType,
    ListingInput,
    ListingStatus,
    MarketStatus,
    PrivacyFlag,
    QualityWarning,
    SafetyDecision,
)
from listing_assistant.text_utils import group_thousands, tr_upper
from listing_assistant.workflow import ListingWorkflow, WorkflowError

S = ListingStatus

STEPS = ["Fotoğraflar", "Bilgi Kontrolü", "Sorular", "İlan Metni", "Onay", "Dışa Aktarım"]
STEP_OF_STATUS = {
    S.DRAFT: 0,
    S.ANALYZING: 0,
    S.FACTS_REVIEW: 1,
    S.NEEDS_INFO: 2,
    S.GENERATING: 3,
    S.SAFETY_CHECK: 3,
    S.BLOCKED: 3,
    S.MODERATION: 3,
    S.READY_FOR_APPROVAL: 4,
    S.APPROVED: 5,
    S.EXPORTED: 6,
}
GROUP_TITLES = {
    FieldGroup.BASIC: "Temel Bilgiler",
    FieldGroup.TECHNICAL: "Teknik Özellikler",
    FieldGroup.APPEARANCE: "Görünüm ve Donanım",
    FieldGroup.CONDITION: "Hasar, Bakım ve Durum",
    FieldGroup.SALE: "Satış Bilgileri",
}
QUALITY_LABELS = {
    QualityWarning.LOW_RESOLUTION: "Düşük çözünürlük",
    QualityWarning.BLURRY: "Bulanık",
    QualityWarning.TOO_DARK: "Karanlık",
    QualityWarning.OVEREXPOSED: "Aşırı parlak",
}
PRIVACY_LABELS = {
    PrivacyFlag.LICENSE_PLATE: "Plaka görünüyor",
    PrivacyFlag.FACE: "Yüz görünüyor",
    PrivacyFlag.DOOR_NUMBER: "Kapı numarası görünüyor",
    PrivacyFlag.DOCUMENT: "Belge görünüyor",
    PrivacyFlag.SCREEN_PERSONAL_INFO: "Ekranda kişisel bilgi var",
}
ISSUE_LABELS = {
    IssueType.UNSUPPORTED_CLAIM: "Dayanaksız ifade",
    IssueType.MISLEADING_STATEMENT: "Yanıltıcı ifade",
    IssueType.SENSITIVE_INFO: "Kişisel veri",
    IssueType.PROHIBITED_PHRASE: "Kesin veya abartılı ifade",
    IssueType.DISCRIMINATORY_LANGUAGE: "Ayrımcı dil",
    IssueType.INCONSISTENCY: "Tutarsızlık",
    IssueType.PROMPT_INJECTION: "Talimat benzeri metin",
}

CSS = """
<style>
.la-steps{display:flex;flex-wrap:wrap;gap:.45rem;margin:.35rem 0 1.4rem}
.la-step{display:flex;align-items:center;gap:.5rem;padding:.3rem .85rem .3rem .35rem;
  border-radius:999px;font-size:.86rem;border:1px solid rgba(128,128,128,.28);opacity:.6}
.la-step b{display:inline-flex;align-items:center;justify-content:center;width:1.45rem;
  height:1.45rem;border-radius:50%;font-size:.74rem;background:rgba(128,128,128,.2)}
.la-step.done{opacity:1}.la-step.done b{background:#15803d;color:#fff}
.la-step.current{opacity:1;font-weight:600;border-color:#2563eb;background:rgba(37,99,235,.1)}
.la-step.current b{background:#2563eb;color:#fff}
.la-step.error{opacity:1;font-weight:600;border-color:#dc2626;background:rgba(220,38,38,.1)}
.la-step.error b{background:#dc2626;color:#fff}
.la-card{border:1px solid rgba(128,128,128,.28);border-radius:14px;padding:1.4rem 1.6rem;
  margin:.25rem 0 .75rem}
.la-kicker{font-size:.72rem;letter-spacing:.09em;opacity:.6;
  margin:0 0 .2rem}
.la-title{font-size:1.4rem;font-weight:700;line-height:1.3;margin:0 0 1.1rem}
.la-specs{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
  gap:.1rem 1.75rem;margin:0 0 .4rem}
.la-specs div{display:flex;justify-content:space-between;gap:1rem;padding:.3rem 0;
  border-bottom:1px dashed rgba(128,128,128,.28);font-size:.93rem}
.la-specs span{opacity:.65}
.la-card h4{font-size:.78rem;letter-spacing:.07em;opacity:.7;
  margin:1.2rem 0 .35rem;padding:0}
.la-card p{margin:0;line-height:1.7}
.la-card ul{columns:2;margin:.2rem 0 0;padding-left:1.1rem}
mark.la-block{background:rgba(220,38,38,.24);color:inherit;border-radius:3px;padding:0 .1rem}
.block-container{padding-top:2.5rem}
mark.la-warn{background:rgba(234,179,8,.28);color:inherit;border-radius:3px;padding:0 .1rem}
</style>
"""

st.set_page_config(page_title="İlan Asistanı", page_icon="🚗", layout="wide")

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>$:])")


def md(text: str) -> str:
    """Escape seller or model text for Streamlit Markdown (no links, formatting or badges)."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


@st.cache_resource
def llm_client(model: str) -> AnthropicLLMClient:
    return AnthropicLLMClient(model)


@contextmanager
def workflow() -> Iterator[ListingWorkflow]:
    settings = load_settings()
    # One connection per script run (Streamlit reruns the script on every interaction),
    # always closed, also when st.rerun() interrupts the run.
    conn = open_database(settings.db_path)
    try:
        yield ListingWorkflow(conn, settings, llm_client(settings.model))
    finally:
        conn.close()


def attempt(action, success: str | None = None):
    """Run one workflow action and show refusals as messages, never as tracebacks."""
    try:
        result = action()
    except (WorkflowError, LLMError, ValueError) as exc:
        st.error(md(str(exc)))
        return None
    except Exception as exc:  # unexpected: show the type only, never internals
        st.error(
            f"Beklenmeyen bir hata oluştu ({type(exc).__name__})."
            " Hiçbir şey onaylanmadı veya dışa aktarılmadı."
        )
        return None
    if success:
        st.session_state["flash"] = success
    st.rerun()
    return result


def listing_name(wf: ListingWorkflow, listing_id: str) -> str:
    values = {f.field_key: f.value for f in wf.approved_facts(listing_id)}
    parts = [wf.schema.display_value(k, values[k]) for k in ("make", "model") if k in values]
    name = " ".join(parts) or "Yeni ilan"
    return f"{name} · {values['model_year']}" if "model_year" in values else name


# --- shared widgets -----------------------------------------------------------------------------


def value_input(
    definition: FieldDefinition,
    *,
    key: str,
    label: str | None = None,
    collapsed: bool = False,
) -> str:
    """The right widget for a field type; always returns the raw string for the workflow."""
    label = label or definition.label_tr + (" *" if definition.required else "")
    visibility = "collapsed" if collapsed else "visible"
    if definition.value_type is ValueType.CHOICE:
        return st.selectbox(
            label,
            ["", *(definition.choices or [])],
            key=key,
            format_func=lambda c: definition.choice_label(c) if c else "Seçiniz",
            label_visibility=visibility,
        )
    if definition.value_type is ValueType.BOOLEAN:
        names = {"": "Seçiniz", "true": definition.boolean_labels[0]}
        names["false"] = definition.boolean_labels[1]
        return st.selectbox(
            label,
            ["", "true", "false"],
            key=key,
            format_func=names.__getitem__,
            label_visibility=visibility,
        )
    placeholder = definition.placeholder or ""
    if definition.multiline:
        return st.text_area(
            label, key=key, placeholder=placeholder, height=90, label_visibility=visibility
        )
    return st.text_input(label, key=key, placeholder=placeholder, label_visibility=visibility)


def field_rows(fields: list[FieldDefinition]) -> Iterator[list[FieldDefinition]]:
    """Pairs of fields, so each row is rendered left-to-right and Tab moves left, right, left."""
    row: list[FieldDefinition] = []
    for definition in fields:
        if definition.multiline:
            if row:
                yield row
                row = []
            yield [definition]
            continue
        row.append(definition)
        if len(row) == 2:
            yield row
            row = []
    if row:
        yield row


def stepper(status: ListingStatus) -> None:
    position = STEP_OF_STATUS[status]
    failed = status in (S.BLOCKED, S.MODERATION)
    items = []
    for index, name in enumerate(STEPS):
        if index < position:
            state, mark = "done", "✓"
        elif index == position:
            state, mark = ("error", "!") if failed else ("current", str(index + 1))
        else:
            state, mark = "", str(index + 1)
        items.append(f'<div class="la-step {state}"><b>{mark}</b>{esc(name)}</div>')
    st.markdown(f'<div class="la-steps">{"".join(items)}</div>', unsafe_allow_html=True)


# --- sidebar ------------------------------------------------------------------------------------


def sidebar(wf: ListingWorkflow) -> str | None:
    st.sidebar.title("🚗 İlan Asistanı")
    st.sidebar.caption("İkinci el araç ilanınızı yalnızca onayladığınız bilgilerden yazar.")
    if st.sidebar.button("➕ Yeni ilan", width="stretch"):
        st.session_state.pop("listing_id", None)
        st.rerun()
    listings = wf.list_listings()
    if listings:
        ids = [listing.id for listing in listings]
        labels = {
            listing.id: f"{listing_name(wf, listing.id)} — {STATUS_LABELS_TR[listing.status]}"
            for listing in listings
        }
        current = st.session_state.get("listing_id")
        choice = st.sidebar.radio(
            "İlanlarım",
            ids,
            index=ids.index(current) if current in ids else None,
            format_func=lambda i: md(labels[i]),
        )
        if choice and choice != current:
            st.session_state["listing_id"] = choice
            st.rerun()
    st.sidebar.divider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        st.sidebar.caption("🔑 Claude API anahtarı bulundu. İlk model çağrısında doğrulanır.")
    else:
        st.sidebar.caption("🔑 Claude API anahtarı bulunamadı. Analiz ve yazım için gerekli.")
    st.sidebar.caption(
        "📊 Piyasa verileri sentetiktir; gerçek fiyat veya fiyat tavsiyesi değildir."
    )
    return st.session_state.get("listing_id")


# --- step views ---------------------------------------------------------------------------------


def create_form(wf: ListingWorkflow) -> None:
    st.title("Yeni araç ilanı")
    st.write(
        "Bildiğiniz bilgileri girin, emin olmadıklarınızı boş bırakın. Eksik zorunlu bilgiler"
        " için size ayrıca soru sorulur."
    )
    values: dict[str, str] = {}
    with st.form("create", border=False):
        for group, title in GROUP_TITLES.items():
            fields = [d for d in wf.schema.field_definitions if d.group is group]
            if not fields:
                continue
            with st.container(border=True):
                st.markdown(f"**{title}**")
                for row in field_rows(fields):
                    if len(row) == 1 and row[0].multiline:
                        values[row[0].key] = value_input(row[0], key=row[0].key)
                        continue
                    for column, definition in zip(st.columns(2), row, strict=False):
                        with column:
                            values[definition.key] = value_input(definition, key=definition.key)
        notes = st.text_area(
            "Serbest notlar (isteğe bağlı)",
            max_chars=2000,
            placeholder=(
                "Eklemek istedikleriniz. Buradan çıkarılan her bilgi size onay için sorulur."
            ),
        )
        confirm = st.checkbox("İletişim bilgisi (telefon / e-posta) eklediğimi biliyorum")
        st.caption("* ile işaretli alanlar zorunludur.")
        submitted = st.form_submit_button("İlanı oluştur", type="primary")
    if submitted:
        data = ListingInput(fields=values, notes=notes, confirm_contact_info=confirm)

        def create():
            result = wf.create_listing(data)
            st.session_state["listing_id"] = result.listing.id
            return result

        attempt(create, "İlan oluşturuldu. Şimdi fotoğrafları ekleyin.")


def photos_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.subheader("Fotoğrafları ekleyin")
    st.caption(
        "JPEG, PNG veya WEBP yükleyin. Konum (GPS) gibi gizli bilgiler otomatik silinir."
        " En iyi sonuç için önden, yandan, arkadan, iç mekândan ve gösterge panelinden çekin."
    )
    with st.form("upload", clear_on_submit=True):
        files = st.file_uploader(
            "Fotoğraf seçin", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True
        )
        uploaded = st.form_submit_button("Yükle")
    if uploaded and files:
        errors = []
        for file in files:
            try:
                wf.add_photo(listing_id, file.getvalue())
            except (WorkflowError, ValueError) as exc:
                errors.append(f"{md(file.name)}: {md(str(exc))}")
        for error in errors:
            st.error(error)
        if not errors:
            st.rerun()
    photo_gallery(wf, listing_id)
    if st.button("Fotoğrafları analiz et", type="primary"):
        with st.spinner("Fotoğraflar inceleniyor…"):
            attempt(
                lambda: wf.run_analysis(listing_id),
                "Analiz tamamlandı. Önerilen bilgileri kontrol edin.",
            )


def photo_gallery(wf: ListingWorkflow, listing_id: str) -> None:
    photos = wf.facts_overview(listing_id).photos
    if not photos:
        return
    columns = st.columns(4)
    for index, photo in enumerate(photos):
        with columns[index % 4]:
            st.image(wf.photo_bytes(listing_id, photo.id), width="stretch")
            notes = [f"#{photo.order_index + 1}"] + (["Kapak"] if photo.is_cover else [])
            notes += [QUALITY_LABELS[w] for w in photo.quality_warnings]
            notes += [f"⚠️ {PRIVACY_LABELS[f]}" for f in photo.privacy_flags]
            st.caption(" · ".join(notes))


def facts_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.subheader("Bilgileri kontrol edin")
    st.caption(
        "Yalnızca onayladığınız bilgiler ilana girer. Fotoğraftan gelen öneriler modelin"
        " tahminidir: doğru değilse reddedin veya düzeltin."
    )
    overview = wf.facts_overview(listing_id)
    if overview.notes_injection_patterns:
        st.warning("Notlarınızda talimat gibi görünen bir ifade var. Sadece veri olarak işlendi.")
    if overview.near_duplicates:
        st.info(f"{len(overview.near_duplicates)} fotoğraf çifti neredeyse aynı görünüyor.")
    for conflict in overview.conflicts:
        label = wf.schema.get(conflict.field_key).label_tr
        st.warning(f"**{label}** için birden fazla değer var: doğru olanı onaylayın.")
    pending = [f for f in overview.facts if f.status is FactStatus.PROPOSED]
    if pending:
        st.info(f"{len(pending)} öneri kararınızı bekliyor.")

    by_field = defaultdict(list)
    for fact in overview.facts:
        by_field[fact.field_key].append(fact)
    for definition in wf.schema.field_definitions:
        if definition.key not in by_field:
            continue
        with st.container(border=True):
            st.markdown(f"**{definition.label_tr}**")
            for fact in by_field[definition.key]:
                fact_row(wf, listing_id, definition, fact)

    with st.expander("Yeni bilgi ekle veya bir alanı değiştir"):
        keys = [d.key for d in wf.schema.field_definitions]
        key = st.selectbox("Alan", keys, format_func=lambda k: wf.schema.get(k).label_tr)
        value = value_input(wf.schema.get(key), key=f"manual-{key}", label="Değer")
        if st.button("Kaydet", key="manual-save"):
            attempt(lambda: wf.set_seller_fact(listing_id, key, value), "Kaydedildi.")

    with st.expander("Fotoğraflar"):
        photo_gallery(wf, listing_id)
    if st.button("Bilgiler doğru, devam et", type="primary"):
        attempt(lambda: wf.complete_fact_review(listing_id))


def fact_row(wf: ListingWorkflow, listing_id: str, definition: FieldDefinition, fact) -> None:
    text, photo, actions = st.columns([4, 2, 3], vertical_alignment="center")
    value = md(definition.display_value(fact.value))
    if fact.source is FactSource.VISION:
        source = f"Fotoğraftan · %{fact.confidence * 100:.0f} güven"
    else:
        source = "Sizin girdiğiniz"
    badge = {
        FactStatus.APPROVED: ":green-badge[Onaylı]",
        FactStatus.REJECTED: ":gray-badge[Reddedildi]",
        FactStatus.PROPOSED: ":orange-badge[Karar bekliyor]",
    }[fact.status]
    shown = f"~~{value}~~" if fact.status is FactStatus.REJECTED else f"**{value}**"
    text.markdown(f"{shown}  \n{badge} :gray[{source}]")
    if fact.evidence_photo_id:
        photo.image(wf.photo_bytes(listing_id, fact.evidence_photo_id), width=110)
    with actions:
        buttons = st.container(horizontal=True)
        if fact.status is not FactStatus.APPROVED:
            label = "Onayla" if fact.status is FactStatus.PROPOSED else "Geri al"
            if buttons.button(label, key=f"approve-{fact.id}"):
                attempt(lambda: wf.approve_fact(listing_id, fact.id))
        if fact.status is FactStatus.PROPOSED and buttons.button("Reddet", key=f"reject-{fact.id}"):
            attempt(lambda: wf.reject_fact(listing_id, fact.id))
        if fact.status is FactStatus.APPROVED and buttons.button("Kaldır", key=f"remove-{fact.id}"):
            attempt(lambda: wf.reject_fact(listing_id, fact.id))
        if fact.status is not FactStatus.REJECTED:
            with buttons.popover("Düzelt"):
                corrected = value_input(definition, key=f"fix-{fact.id}", label="Doğru değer")
                if st.button("Kaydet", key=f"correct-{fact.id}"):
                    attempt(lambda: wf.correct_fact(listing_id, fact.id, corrected))


def questions_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.subheader("Birkaç sorumuz var")
    st.caption("Bunlar ilan için gerekli ama eksik olan bilgiler. Bilmiyorsanız geçebilirsiniz.")
    confirm = st.checkbox("Cevaplarımda bilerek iletişim bilgisi veriyorum")
    for question in wf.open_questions(listing_id):
        with st.container(border=True):
            answer = value_input(
                wf.schema.get(question.field_key),
                key=f"answer-{question.id}",
                label=md(question.question),
            )
            buttons = st.container(horizontal=True)
            if buttons.button("Cevapla", key=f"send-{question.id}", type="primary"):
                attempt(
                    lambda qid=question.id, text=answer: wf.answer_question(
                        listing_id, qid, text, confirm_contact_info=confirm
                    )
                )
            if buttons.button("Bilmiyorum, geç", key=f"skip-{question.id}"):
                attempt(
                    lambda qid=question.id: wf.answer_question(listing_id, qid, None, decline=True)
                )


def generate_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.subheader("İlan metnini yazdırın")
    count = len(wf.approved_facts(listing_id))
    st.caption(
        f"Yazar yalnızca onayladığınız {count} bilgiyi görür. Ardından ayrı bir denetçi"
        " her cümlenin bir bilgiye dayandığını kontrol eder."
    )
    if st.button("İlan metnini yaz", type="primary"):
        with st.spinner("İlan yazılıyor ve kontrol ediliyor…"):
            attempt(lambda: wf.run_generation(listing_id))


def highlight(text: str, issues) -> str:
    """Mark a sentence that has review issues: red for a blocker, yellow for a warning."""
    if not issues:
        return esc(text)
    blocking = any(i.severity is IssueSeverity.BLOCK for i in issues)
    return f'<mark class="{"la-block" if blocking else "la-warn"}">{esc(text)}</mark>'


def listing_preview(wf: ListingWorkflow, listing_id: str, draft, issues_by_claim) -> None:
    rendered = wf.rendered_listing(listing_id, draft)
    parts = [
        '<div class="la-card">',
        '<p class="la-kicker">İLAN BAŞLIĞI</p>',
        f'<div class="la-title">{highlight(draft.title, issues_by_claim.get(0))}</div>',
    ]
    if rendered.specs:
        parts.append(f"<h4>{esc(tr_upper(SPEC_HEADING))}</h4><div class='la-specs'>")
        parts += [
            f"<div><span>{esc(row.label)}</span><strong>{esc(row.value)}</strong></div>"
            for row in rendered.specs
        ]
        parts.append("</div>")
    sentences = list(enumerate(draft.content.sentences, start=1))
    for section, heading in SECTION_HEADINGS.items():
        in_section = [(i, c) for i, c in sentences if c.section is section]
        if in_section:
            body = " ".join(highlight(c.text, issues_by_claim.get(i)) for i, c in in_section)
            parts.append(f"<h4>{esc(tr_upper(heading))}</h4><p>{body}</p>")
    if rendered.equipment:
        items = "".join(f"<li>{esc(item)}</li>" for item in rendered.equipment)
        parts.append(f"<h4>{esc(tr_upper(EQUIPMENT_HEADING))}</h4><ul>{items}</ul>")
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def draft_view(wf: ListingWorkflow, listing_id: str):
    draft, verdicts = wf.latest_draft(listing_id)
    if draft is None:
        st.info("Henüz bir taslak yok.")
        return None, []
    issues = [issue for verdict in verdicts for issue in verdict.issues]
    by_claim = defaultdict(list)
    for issue in issues:
        if issue.claim_index is not None:
            by_claim[issue.claim_index].append(issue)
    listing_preview(wf, listing_id, draft, by_claim)
    st.caption(f"Taslak sürüm {draft.version}")

    claims = draft.all_claims
    if issues:
        st.markdown("#### Kontrol sonuçları")
    for issue in issues:
        blocking = issue.severity is IssueSeverity.BLOCK
        badge = ":red-badge[Engel]" if blocking else ":orange-badge[Uyarı]"
        with st.container(border=True):
            st.markdown(f"{badge} **{ISSUE_LABELS[issue.issue_type]}**")
            if issue.claim_index is not None and issue.claim_index < len(claims):
                st.markdown(f"> {md(claims[issue.claim_index].text)}")
            st.markdown(md(seller_message(issue.message)))
    if not issues and verdicts:
        st.success("Metin kontrolden sorunsuz geçti.")

    facts = {f.id: f for f in wf.facts_overview(listing_id).facts}
    with st.expander("Her cümle hangi bilgiye dayanıyor?"):
        for index, claim in enumerate(claims):
            sources = ", ".join(
                f"{wf.schema.get(facts[i].field_key).label_tr}: "
                f"{wf.schema.display_value(facts[i].field_key, facts[i].value)}"
                for i in claim.fact_ids
                if i in facts
            )
            name = "Başlık" if index == 0 else f"{index}. cümle"
            st.markdown(f"**{name}:** {md(claim.text)}  \n:gray[{md(sources)}]")
    return draft, verdicts


def market_panel(wf: ListingWorkflow, listing_id: str) -> None:
    with st.expander("Piyasa fiyat aralığı (sentetik veri, sadece bilgi amaçlı)"):
        st.caption(
            "Rastgele üretilmiş sentetik ilanlardan hesaplanır. Gerçek piyasa fiyatı ve fiyat"
            " tavsiyesi değildir; ilan metnine eklenmez."
        )
        key = f"market-{listing_id}"
        if st.button("Hesapla", key=f"btn-{key}"):
            st.session_state[key] = wf.market_summary(listing_id)
        summary = st.session_state.get(key)
        if summary is None:
            return
        if summary.status is MarketStatus.OK:
            st.write(
                f"Ortanca {group_thousands(summary.median_price_try)} TL · ilanların orta yarısı "
                f"{group_thousands(summary.q1_price_try)}–{group_thousands(summary.q3_price_try)}"
                f" TL ({summary.sample_size} sentetik ilan)"
            )
        elif summary.status is MarketStatus.MISSING_FACTS:
            st.write("Hesaplamak için marka, model ve model yılı gerekli.")
        else:
            st.write("Yeterli sayıda benzer sentetik ilan yok.")
        for note in summary.widening_notes:
            st.caption(note)


def approval_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.subheader("Son kontrol ve onay")
    st.caption(
        "İlanınız dışa aktarılacağı hâliyle aşağıda. Onayınız yalnızca bu sürüm için geçerlidir."
    )
    draft, verdicts = draft_view(wf, listing_id)
    if draft is None:
        return
    with st.expander("Fotoğraf sırası"):
        photo_gallery(wf, listing_id)
    market_panel(wf, listing_id)
    warn = combined_decision(verdicts) is SafetyDecision.WARN
    flagged = any(p.privacy_flags for p in wf.facts_overview(listing_id).photos)
    ack_warnings = (
        st.checkbox("Uyarıları okudum ve bu hâliyle yayımlamak istiyorum") if warn else False
    )
    ack_privacy = (
        st.checkbox(
            "Bazı fotoğraflarda plaka, yüz veya belge görünebilir; bunu biliyor ve kabul ediyorum"
        )
        if flagged
        else False
    )
    if st.button("Bu sürümü onayla", type="primary"):
        attempt(
            lambda: wf.approve_final(
                listing_id,
                draft.version,
                acknowledge_warnings=ack_warnings,
                acknowledge_privacy_flags=ack_privacy,
            ),
            "İlan onaylandı. Artık dışa aktarabilirsiniz.",
        )
    with st.expander("Değişiklik iste veya bilgilere dön"):
        change_request_form(wf, listing_id, "changes")
        st.divider()
        st.caption("Bir bilgiyi eklemek, değiştirmek veya kaldırmak istiyorsanız:")
        if st.button("Bilgilere geri dön", key="reopen-approval"):
            attempt(lambda: wf.reopen_facts(listing_id))


def change_request_form(wf: ListingWorkflow, listing_id: str, key: str) -> None:
    with st.form(key, border=False):
        comment = st.text_area(
            "Yazara ne değiştirmesini istersiniz?",
            max_chars=1000,
            placeholder="örn. Hasarla ilgili cümleyi çıkar, başlığa yakıt tipini ekle.",
        )
        if st.form_submit_button("Yeniden yazdır"):
            with st.spinner("İlan yeniden yazılıyor…"):
                attempt(lambda: wf.request_changes(listing_id, comment))


def blocked_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.subheader("Taslakta düzeltilmesi gereken bir sorun var")
    st.warning(
        "Güvenlik kontrolü, iki düzeltme denemesinden sonra da bir kuralın çiğnendiğini gördü"
        " ve taslağı durdurdu. Baştan başlamanız gerekmiyor: aşağıdaki iki yoldan biriyle"
        " devam edebilirsiniz."
    )
    draft_view(wf, listing_id)
    fix_facts, instruct = st.columns(2)
    with fix_facts, st.container(border=True):
        st.markdown("**1. Bilgileri düzeltin**")
        st.caption(
            "İşaretli cümlenin dayandığı bilgiyi değiştirin veya kaldırın. Sonra ilan yeniden"
            " yazılır."
        )
        if st.button("Bilgilere dön", key="reopen-blocked", type="primary"):
            attempt(lambda: wf.reopen_facts(listing_id))
    with instruct, st.container(border=True):
        st.markdown("**2. Yazara talimat verin**")
        st.caption("Neyin değişmesi gerektiğini yazın; ilan bu talimatla yeniden yazılır.")
        change_request_form(wf, listing_id, "blocked-changes")


def export_step(wf: ListingWorkflow, listing_id: str) -> None:
    listing = wf.get_listing(listing_id)
    key = f"export-{listing_id}"
    if listing.status is S.APPROVED:
        st.subheader("İlanınız onaylandı")
        st.caption(
            "Dışa aktarınca başlığı ve açıklamayı kopyalayıp ilan sitesine yapıştırabilirsiniz."
        )
        if st.button("İlanı dışa aktar", type="primary"):
            try:
                st.session_state[key] = wf.export_listing(listing_id)
            except (WorkflowError, ValueError) as exc:
                st.error(md(str(exc)))
    elif key not in st.session_state:
        st.session_state[key] = wf.export_listing(listing_id)
    package = st.session_state.get(key)
    if not package:
        return
    st.subheader("İlanınız hazır")
    st.caption(
        "Başlığı ve açıklamayı sağ üstteki kopyala simgesiyle alıp ilan sitesindeki ilgili"
        " kutulara yapıştırın. Fotoğrafları ZIP içindeki sırayla yükleyin."
    )
    st.markdown("**İlan başlığı**")
    st.code(package.title, language=None, wrap_lines=True)
    st.markdown("**İlan açıklaması**")
    st.code(package.description, language=None, wrap_lines=True)
    st.download_button(
        "İlan metni ve sıralı fotoğrafları indir (.zip)",
        package.zip_bytes,
        file_name=f"ilan-{listing_id[:8]}.zip",
        mime="application/zip",
        type="primary",
    )
    with st.expander("Fotoğraf sırası"):
        photo_gallery(wf, listing_id)


def moderation_step(wf: ListingWorkflow, listing_id: str) -> None:
    st.info("Bu ilan moderatör incelemesinde. Bu sürümde moderatör paneli yok.")
    draft_view(wf, listing_id)


VIEWS = {
    S.DRAFT: photos_step,
    S.ANALYZING: photos_step,
    S.FACTS_REVIEW: facts_step,
    S.NEEDS_INFO: questions_step,
    S.GENERATING: generate_step,
    S.SAFETY_CHECK: generate_step,
    S.READY_FOR_APPROVAL: approval_step,
    S.BLOCKED: blocked_step,
    S.MODERATION: moderation_step,
    S.APPROVED: export_step,
    S.EXPORTED: export_step,
}


def main() -> None:
    with workflow() as wf:
        render(wf)


def render(wf: ListingWorkflow) -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    listing_id = sidebar(wf)
    if flash := st.session_state.pop("flash", None):
        st.success(flash)
    if listing_id is None:
        create_form(wf)
        return
    listing = wf.get_listing(listing_id)
    st.title(listing_name(wf, listing_id))
    st.caption(f"İlan no {listing.id[:8]} · {STATUS_LABELS_TR[listing.status]}")
    stepper(listing.status)
    if listing.status is S.ANALYZING:
        st.warning("Son analiz tamamlanamadı. Tekrar başlatabilirsiniz.")
    VIEWS[listing.status](wf, listing_id)


main()
