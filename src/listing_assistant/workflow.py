"""The orchestrator and service layer: the security boundary of the application.

Deterministic code decides what runs next (architecture doc §4); models only work inside
agents. Every public method is a human action or a step the human started, and each one
checks the listing's status itself, so the UI cannot skip steps by calling methods in a
different order. Agents never receive the database or a listing ID they could change:
they get an `AgentRun` whose tools are bound to one listing.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from listing_assistant.agents.copywriter import run_copywriter
from listing_assistant.agents.fact_reconciler import (
    Conflict,
    ReconciliationReport,
    find_conflicts,
    run_fact_reconciler,
)
from listing_assistant.agents.gap_detector import find_gaps, run_gap_detector
from listing_assistant.agents.intake_guard import (
    InputRejectedError,
    IntakeFindings,
    check_user_text,
)
from listing_assistant.agents.market_analyst import run_market_analyst
from listing_assistant.agents.photo_curator import CurationResult, run_photo_curator
from listing_assistant.agents.safety_reviewer import combined_decision, run_safety_reviewer
from listing_assistant.agents.vision_analyst import VisionReport, run_vision_analyst
from listing_assistant.config import Settings
from listing_assistant.db.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    AuditLogRepository,
    ClarificationRepository,
    DraftRepository,
    FactRepository,
    ListingRepository,
    NoteRepository,
    PhotoPresentationRepository,
    PhotoRepository,
    SafetyReviewRepository,
)
from listing_assistant.export import ExportPackage, build_export
from listing_assistant.field_schema import CategorySchema, load_category_schema
from listing_assistant.images import find_near_duplicates
from listing_assistant.injection import find_injection_patterns
from listing_assistant.listing_format import RenderedListing, render_listing
from listing_assistant.llm import BudgetExceededError, LlmBudget, LLMClient
from listing_assistant.models import (
    STATUS_LABELS_TR,
    ActorType,
    AgentName,
    Approval,
    ApprovalDecision,
    ApprovalGate,
    AuditEntry,
    Category,
    Clarification,
    ClarificationStatus,
    Draft,
    Fact,
    FactSource,
    FactStatus,
    IssueSeverity,
    Listing,
    ListingInput,
    ListingPhoto,
    ListingStatus,
    MarketSummary,
    SafetyDecision,
    SafetyVerdict,
)
from listing_assistant.photo_intake import ingest_photo, photo_path
from listing_assistant.state_machine import transition
from listing_assistant.synthetic_market import seed_if_empty
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import ListingTools

S = ListingStatus
# Architecture doc §4: at most two correction rounds after the first draft.
MAX_CORRECTION_ROUNDS = 2
LOCAL_ACTOR = "local_user"  # no authentication in this MVP: a single local seller


class WorkflowError(Exception):
    """A refused action. Messages are safe to show to the seller."""


class GenerationFailedError(WorkflowError):
    pass


@dataclass(frozen=True)
class CreateResult:
    listing: Listing
    warnings: tuple[str, ...]
    injection_patterns: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisReport:
    vision: VisionReport
    curation: CurationResult
    reconciliation: ReconciliationReport


@dataclass(frozen=True)
class FactsOverview:
    facts: tuple[Fact, ...]
    conflicts: tuple[Conflict, ...]
    photos: tuple[ListingPhoto, ...]
    near_duplicates: tuple[tuple[str, str, int], ...]
    notes_injection_patterns: tuple[str, ...]


@dataclass(frozen=True)
class GenerationResult:
    status: ListingStatus
    draft: Draft | None
    verdicts: tuple[SafetyVerdict, ...]
    correction_rounds: int


class ListingWorkflow:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        llm: LLMClient,
        schema: CategorySchema | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._llm = llm
        self._schema = schema or load_category_schema(Category.CAR)
        self._listings = ListingRepository(conn)
        self._facts = FactRepository(conn)
        self._photos = PhotoRepository(conn)
        self._drafts = DraftRepository(conn)
        self._runs = AgentRunRepository(conn)
        self._audit_log = AuditLogRepository(conn)
        self._clarifications = ClarificationRepository(conn)
        seed_if_empty(conn)

    @property
    def schema(self) -> CategorySchema:
        return self._schema

    # --- step 1: create a listing with the seller's form input --------------------------

    def create_listing(self, data: ListingInput) -> CreateResult:
        listing = Listing(category=data.category)
        run = self._new_run(AgentName.INTAKE_GUARD, listing.id, LlmBudget(0, 0))
        facts, findings = [], []
        # Validate everything before writing anything, so a refusal leaves no trace.
        for key, raw in data.fields.items():
            if not raw.strip():
                continue
            value = self._normalize(key, raw)
            findings.append(
                check_user_text(run, value, contact_confirmed=data.confirm_contact_info)
            )
            facts.append(
                Fact(
                    listing_id=listing.id,
                    field_key=key,
                    value=value,
                    source=FactSource.USER,
                    status=FactStatus.APPROVED,
                )
            )
        findings.append(
            check_user_text(run, data.notes, contact_confirmed=data.confirm_contact_info)
        )

        self._listings.add(listing)
        for fact in facts:
            self._facts.add(fact)
        if data.notes.strip():
            NoteRepository(self._conn).add(listing.id, data.notes.strip())
        self._runs.save(run.to_record(), run.tool_calls)
        warnings = tuple(w for f in findings for w in f.warnings)
        patterns = tuple(dict.fromkeys(p for f in findings for p in f.injection_patterns))
        self._audit(
            "listing.create",
            "listing",
            listing.id,
            fields=len(facts),
            has_notes=bool(data.notes.strip()),
        )
        if patterns:
            self._audit(
                "security.injection_pattern",
                "listing",
                listing.id,
                actor_type=ActorType.SYSTEM,
                patterns=",".join(patterns),
            )
        return CreateResult(listing, warnings, patterns)

    def add_photo(self, listing_id: str, raw: bytes) -> ListingPhoto:
        self._require(listing_id, S.DRAFT)
        return ingest_photo(self._conn, self._settings, listing_id, raw)

    # --- step 2: analysis (vision -> curation -> reconciliation) ------------------------

    def run_analysis(self, listing_id: str) -> AnalysisReport:
        listing = self._require(listing_id, S.DRAFT, S.ANALYZING)
        photo_count = self._photos.count_for_listing(listing_id)
        if photo_count == 0:
            raise WorkflowError("Analizden önce en az bir fotoğraf yükleyin.")
        notes = NoteRepository(self._conn).get(listing_id)
        budget = self._budget(listing_id)
        needed = photo_count + (1 if notes else 0)
        if budget.remaining < needed:
            raise BudgetExceededError(
                f"Analiz için {needed} model çağrısı gerekiyor, ama sadece"
                f" {budget.remaining} hakkınız kaldı."
            )
        if listing.status is S.DRAFT:
            transition(self._conn, listing_id, S.ANALYZING)

        with self._agent(AgentName.VISION_ANALYST, listing_id, budget) as run:
            vision = run_vision_analyst(run, self._schema)
        for result in vision.photos:
            if result.instruction_text_detected:
                self._audit(
                    "security.instruction_text_in_photo",
                    "listing_photo",
                    result.photo_id,
                    actor_type=ActorType.AGENT,
                    listing_id=listing_id,
                )

        with self._agent(AgentName.PHOTO_CURATOR, listing_id, budget) as run:
            curation = run_photo_curator(run, vision)
        presentation = PhotoPresentationRepository(self._conn)
        for index, photo_id in enumerate(curation.order):
            presentation.update(
                listing_id,
                photo_id,
                order_index=index,
                is_cover=index == 0,
                privacy_flags=[flag.value for flag in curation.privacy_flags.get(photo_id, ())],
            )

        with self._agent(AgentName.FACT_RECONCILER, listing_id, budget) as run:
            reconciliation = run_fact_reconciler(run, self._schema, vision.proposals, notes)

        transition(self._conn, listing_id, S.FACTS_REVIEW)
        return AnalysisReport(vision, curation, reconciliation)

    # --- step 3: approval gate 1, the seller reviews every proposed fact ----------------

    def facts_overview(self, listing_id: str) -> FactsOverview:
        self._require_exists(listing_id)
        facts = self._facts.list_for_listing(listing_id)
        photos = self._photos.list_for_listing(listing_id)
        notes = NoteRepository(self._conn).get(listing_id)
        return FactsOverview(
            facts=tuple(facts),
            conflicts=tuple(find_conflicts(facts)),
            photos=tuple(photos),
            near_duplicates=tuple(
                find_near_duplicates([(p.id, p.perceptual_hash) for p in photos])
            ),
            notes_injection_patterns=tuple(find_injection_patterns(notes)),
        )

    def approve_fact(self, listing_id: str, fact_id: str) -> Fact:
        self._require(listing_id, S.FACTS_REVIEW)
        fact = self._facts.approve(listing_id, fact_id)
        self._audit(
            "fact.approve",
            "fact",
            fact_id,
            listing_id=listing_id,
            field=fact.field_key,
            source=fact.source.value,
        )
        return fact

    def reject_fact(self, listing_id: str, fact_id: str) -> None:
        self._require(listing_id, S.FACTS_REVIEW)
        self._facts.set_status(listing_id, fact_id, FactStatus.REJECTED)
        self._audit("fact.reject", "fact", fact_id, listing_id=listing_id)

    def correct_fact(
        self, listing_id: str, fact_id: str, value: str, *, confirm_contact_info: bool = False
    ) -> Fact:
        self._require(listing_id, S.FACTS_REVIEW)
        original = self._facts.get(listing_id, fact_id)
        if original is None:
            raise WorkflowError("Bu bilgi bu ilana ait değil.")
        new = self._seller_fact(listing_id, original.field_key, value, confirm_contact_info)
        # add_approved already replaced an approved original; a proposal is rejected here.
        if original.status is FactStatus.PROPOSED:
            self._facts.set_status(listing_id, fact_id, FactStatus.REJECTED)
        self._audit(
            "fact.correct",
            "fact",
            new.id,
            listing_id=listing_id,
            replaces=fact_id,
            field=new.field_key,
        )
        return new

    def set_seller_fact(
        self, listing_id: str, field_key: str, value: str, *, confirm_contact_info: bool = False
    ) -> Fact:
        """Add or replace a value the seller types during review (e.g. an optional field)."""
        self._require(listing_id, S.FACTS_REVIEW)
        fact = self._seller_fact(listing_id, field_key, value, confirm_contact_info)
        self._audit("fact.set_by_seller", "fact", fact.id, listing_id=listing_id, field=field_key)
        return fact

    def complete_fact_review(self, listing_id: str) -> ListingStatus:
        self._require(listing_id, S.FACTS_REVIEW)
        pending = self._facts.list_for_listing(listing_id, FactStatus.PROPOSED)
        if pending:
            raise WorkflowError(
                f"{len(pending)} öneri daha karar bekliyor: her birini onaylayın ya da reddedin."
            )
        self._on_facts_gate_passed(listing_id)
        with self._agent(AgentName.GAP_DETECTOR, listing_id) as run:
            questions = run_gap_detector(run, self._schema, self._declined_keys(listing_id))
        target = S.NEEDS_INFO if questions else S.GENERATING
        transition(self._conn, listing_id, target)
        return target

    def _on_facts_gate_passed(self, listing_id: str) -> None:
        """Approval gate 1 (architecture doc §7.6): recorded before the flow may continue."""
        ApprovalRepository(self._conn).add(
            Approval(
                listing_id=listing_id,
                gate=ApprovalGate.FACTS,
                decision=ApprovalDecision.APPROVED,
                actor_name=LOCAL_ACTOR,
            )
        )
        self._audit("approval.facts", "listing", listing_id)

    # --- step 4: clarification loop -----------------------------------------------------

    def open_questions(self, listing_id: str) -> list[Clarification]:
        self._require_exists(listing_id)
        return self._clarifications.list_for_listing(listing_id, ClarificationStatus.OPEN)

    def answer_question(
        self,
        listing_id: str,
        clarification_id: str,
        answer: str | None,
        *,
        decline: bool = False,
        confirm_contact_info: bool = False,
    ) -> ListingStatus:
        self._require(listing_id, S.NEEDS_INFO)
        question = self._clarifications.get(listing_id, clarification_id)
        if question is None or question.status is not ClarificationStatus.OPEN:
            raise WorkflowError("Bu soru artık açık değil.")
        if decline:
            self._clarifications.close(listing_id, clarification_id, ClarificationStatus.DECLINED)
            self._audit(
                "clarification.decline",
                "clarification",
                clarification_id,
                listing_id=listing_id,
                field=question.field_key,
            )
        else:
            if not answer or not answer.strip():
                raise WorkflowError("Bir cevap yazın ya da 'Bilmiyorum, geç' seçeneğini kullanın.")
            fact = self._seller_fact(listing_id, question.field_key, answer, confirm_contact_info)
            self._clarifications.close(
                listing_id, clarification_id, ClarificationStatus.ANSWERED, fact.id
            )
            self._audit(
                "clarification.answer",
                "clarification",
                clarification_id,
                listing_id=listing_id,
                field=question.field_key,
            )

        if self.open_questions(listing_id):
            return S.NEEDS_INFO
        approved = self._facts.list_for_listing(listing_id, FactStatus.APPROVED)
        if find_gaps(self._schema, approved, self._declined_keys(listing_id)):
            with self._agent(AgentName.GAP_DETECTOR, listing_id) as run:
                run_gap_detector(run, self._schema, self._declined_keys(listing_id))
            return S.NEEDS_INFO
        transition(self._conn, listing_id, S.GENERATING)
        return S.GENERATING

    # --- step 5: generation with bounded correction rounds ------------------------------

    def run_generation(
        self,
        listing_id: str,
        change_request: str | None = None,
        previous_draft: Draft | None = None,
    ) -> GenerationResult:
        listing = self._require(listing_id, S.GENERATING, S.SAFETY_CHECK)
        if not self._facts.list_for_listing(listing_id, FactStatus.APPROVED):
            raise WorkflowError(
                "İlan metni yazılamaz: onaylanmış hiçbir bilgi yok. En az bir bilgi gerekiyor."
            )
        budget = self._budget(listing_id)
        # Resume: a crash between writing and reviewing leaves the draft to be reviewed.
        pending = self._drafts.latest(listing_id) if listing.status is S.SAFETY_CHECK else None
        feedback: list[str] = []
        rounds = 0
        while True:
            if pending is None:
                with self._agent(AgentName.COPYWRITER, listing_id, budget) as run:
                    attempt = run_copywriter(
                        run, self._schema, feedback, change_request, previous_draft
                    )
                if attempt.draft is None:
                    rounds += 1
                    if rounds > MAX_CORRECTION_ROUNDS:
                        raise GenerationFailedError(
                            "İlan yazarı yalnızca onaylı bilgilere dayanan bir taslak üretemedi."
                            " Tekrar deneyin."
                        )
                    feedback = list(attempt.problems)
                    continue
                pending = attempt.draft
                transition(self._conn, listing_id, S.SAFETY_CHECK)

            with self._agent(AgentName.SAFETY_REVIEWER, listing_id, budget) as run:
                verdicts = run_safety_reviewer(run, pending, self._schema)
            reviews = SafetyReviewRepository(self._conn)
            for verdict in verdicts:
                reviews.add(verdict)
            decision = combined_decision(verdicts)

            if decision is not SafetyDecision.BLOCK:
                transition(self._conn, listing_id, S.READY_FOR_APPROVAL)
                return GenerationResult(S.READY_FOR_APPROVAL, pending, tuple(verdicts), rounds)

            blocking = [i for v in verdicts for i in v.issues if i.severity is IssueSeverity.BLOCK]
            if all(i.fixable for i in blocking) and rounds < MAX_CORRECTION_ROUNDS:
                rounds += 1
                feedback = [
                    (f"Claim {i.claim_index}: " if i.claim_index is not None else "") + i.message
                    for i in blocking
                ]
                # The Copywriter sees what it wrote, so "Claim 3" points at a real sentence.
                previous_draft, pending = pending, None
                transition(self._conn, listing_id, S.GENERATING)
                continue

            transition(self._conn, listing_id, S.BLOCKED)
            self._audit(
                "listing.blocked",
                "draft",
                pending.id,
                actor_type=ActorType.SYSTEM,
                listing_id=listing_id,
                issues=len(blocking),
            )
            return GenerationResult(S.BLOCKED, pending, tuple(verdicts), rounds)

    # --- step 6: approval gate 2 and export ---------------------------------------------

    def approve_final(
        self,
        listing_id: str,
        draft_version: int,
        *,
        acknowledge_warnings: bool = False,
        acknowledge_privacy_flags: bool = False,
    ) -> Approval:
        """The seller approves exactly the draft version they looked at."""
        self._require(listing_id, S.READY_FOR_APPROVAL)
        draft, verdicts = self.latest_draft(listing_id)
        if draft is None or draft.version != draft_version:
            raise WorkflowError("Daha yeni bir taslak var. Önce en son sürümü inceleyin.")
        decision = combined_decision(verdicts)
        if decision is SafetyDecision.BLOCK or not verdicts:
            raise WorkflowError("Bu taslak güvenlik kontrolünden geçmedi.")
        if decision is SafetyDecision.WARN and not acknowledge_warnings:
            raise WorkflowError("Onaylamadan önce uyarıları okuyup onay kutusunu işaretleyin.")
        flagged = [p for p in self._photos.list_for_listing(listing_id) if p.privacy_flags]
        if flagged and not acknowledge_privacy_flags:
            raise WorkflowError(
                f"{len(flagged)} fotoğrafta kişisel bir ayrıntı (plaka, yüz, belge)"
                " görünüyor olabilir ya da fotoğraf incelenemedi."
                " Bulanıklaştırın ya da onaylamadan önce bunu kabul ettiğinizi işaretleyin."
            )
        approval = ApprovalRepository(self._conn).add(
            Approval(
                listing_id=listing_id,
                gate=ApprovalGate.FINAL,
                decision=ApprovalDecision.APPROVED,
                draft_id=draft.id,
                actor_name=LOCAL_ACTOR,
            )
        )
        transition(
            self._conn, listing_id, S.APPROVED, actor_type=ActorType.USER, actor_name=LOCAL_ACTOR
        )
        self._audit(
            "approval.final",
            "draft",
            draft.id,
            listing_id=listing_id,
            version=draft.version,
            warnings_acknowledged=acknowledge_warnings,
            privacy_acknowledged=acknowledge_privacy_flags,
        )
        return approval

    def request_changes(self, listing_id: str, comment: str) -> GenerationResult:
        """Rewrite the latest draft with the seller's instructions (also after a block)."""
        self._require(listing_id, S.READY_FOR_APPROVAL, S.BLOCKED)
        comment = comment.strip()
        if not comment:
            raise WorkflowError("Neyin değişmesini istediğinizi yazın.")
        if len(comment) > 1000:
            raise WorkflowError("Değişiklik isteği en fazla 1000 karakter olabilir.")
        self._guard(listing_id, comment, contact_confirmed=False)
        draft, _ = self.latest_draft(listing_id)
        ApprovalRepository(self._conn).add(
            Approval(
                listing_id=listing_id,
                gate=ApprovalGate.FINAL,
                decision=ApprovalDecision.CHANGES_REQUESTED,
                draft_id=draft.id,
                comment=comment,
                actor_name=LOCAL_ACTOR,
            )
        )
        transition(
            self._conn, listing_id, S.GENERATING, actor_type=ActorType.USER, actor_name=LOCAL_ACTOR
        )
        return self.run_generation(listing_id, change_request=comment, previous_draft=draft)

    def export_listing(self, listing_id: str) -> ExportPackage:
        listing = self._require(listing_id, S.APPROVED, S.EXPORTED)
        draft, _ = self.latest_draft(listing_id)
        approvals = ApprovalRepository(self._conn)
        # Defence in depth: the status alone is not trusted; the approval record is checked.
        if draft is None or approvals.final_approval_for(listing_id, draft.id) is None:
            raise WorkflowError("Dışa aktarmak için en son taslağı onaylamanız gerekiyor.")
        photos = self._photos.list_for_listing(listing_id)
        package = build_export(
            self.rendered_listing(listing_id, draft),
            draft.version,
            [self.photo_bytes(listing_id, p.id) for p in photos],
        )
        if listing.status is S.APPROVED:
            transition(
                self._conn,
                listing_id,
                S.EXPORTED,
                actor_type=ActorType.USER,
                actor_name=LOCAL_ACTOR,
            )
        self._audit(
            "listing.export",
            "draft",
            draft.id,
            listing_id=listing_id,
            version=draft.version,
            photos=package.photo_count,
        )
        return package

    def reopen_facts(self, listing_id: str) -> ListingStatus:
        """Go back to fact review from a blocked or not-yet-approved draft.

        The seller fixes the facts behind a problem instead of starting a new listing.
        Existing drafts stay as history; the next draft gets a new version number, and
        gate 1 is recorded again when the seller finishes the review.
        """
        self._require(listing_id, S.BLOCKED, S.READY_FOR_APPROVAL)
        transition(
            self._conn,
            listing_id,
            S.FACTS_REVIEW,
            actor_type=ActorType.USER,
            actor_name=LOCAL_ACTOR,
        )
        self._audit("listing.reopen_facts", "listing", listing_id)
        return S.FACTS_REVIEW

    # --- read models for the UI -----------------------------------------------------------

    def approved_facts(self, listing_id: str) -> list[Fact]:
        self._require_exists(listing_id)
        return self._facts.list_for_listing(listing_id, FactStatus.APPROVED)

    def rendered_listing(self, listing_id: str, draft: Draft | None = None) -> RenderedListing:
        """The listing exactly as it will be exported (title, specs, sections, equipment)."""
        draft = draft or self._drafts.latest(listing_id)
        if draft is None or draft.listing_id != listing_id:
            raise WorkflowError("Bu ilan için henüz bir taslak yok.")
        facts = self._facts.list_for_listing(listing_id, FactStatus.APPROVED)
        return render_listing(draft, facts, self._schema)

    def market_summary(self, listing_id: str) -> MarketSummary:
        self._require_exists(listing_id)
        with self._agent(AgentName.MARKET_ANALYST, listing_id, LlmBudget(0, 0)) as run:
            return run_market_analyst(run)

    def latest_draft(self, listing_id: str) -> tuple[Draft | None, list[SafetyVerdict]]:
        self._require_exists(listing_id)
        draft = self._drafts.latest(listing_id)
        if draft is None:
            return None, []
        return draft, SafetyReviewRepository(self._conn).list_for_draft(draft.id)

    def list_listings(self) -> list[Listing]:
        return self._listings.list_all()

    def get_listing(self, listing_id: str) -> Listing:
        return self._require_exists(listing_id)

    def photo_bytes(self, listing_id: str, photo_id: str) -> bytes:
        photo = self._photos.get(listing_id, photo_id)
        if photo is None:
            raise WorkflowError("Bu fotoğraf bu ilana ait değil.")
        return photo_path(self._settings, photo.storage_key).read_bytes()

    # --- internals ----------------------------------------------------------------------

    def _require_exists(self, listing_id: str) -> Listing:
        listing = self._listings.get(listing_id)
        if listing is None:
            raise WorkflowError("İlan bulunamadı.")
        return listing

    def _require(self, listing_id: str, *allowed: ListingStatus) -> Listing:
        listing = self._require_exists(listing_id)
        if listing.status not in allowed:
            raise WorkflowError(
                f"Bu işlem ilan '{STATUS_LABELS_TR[listing.status]}' aşamasındayken yapılamaz."
            )
        return listing

    def _normalize(self, key: str, raw: str) -> str:
        try:
            return self._schema.normalize_value(key, raw)
        except ValueError as exc:
            raise InputRejectedError(str(exc)) from None

    def _seller_fact(self, listing_id: str, key: str, raw: str, contact_confirmed: bool) -> Fact:
        value = self._normalize(key, raw)
        self._guard(listing_id, value, contact_confirmed)
        return self._facts.add_approved(
            Fact(
                listing_id=listing_id,
                field_key=key,
                value=value,
                source=FactSource.USER,
                status=FactStatus.APPROVED,
            )
        )

    def _guard(self, listing_id: str, text: str, contact_confirmed: bool) -> IntakeFindings:
        with self._agent(AgentName.INTAKE_GUARD, listing_id, LlmBudget(0, 0)) as run:
            return check_user_text(run, text, contact_confirmed=contact_confirmed)

    def _declined_keys(self, listing_id: str) -> set[str]:
        declined = self._clarifications.list_for_listing(listing_id, ClarificationStatus.DECLINED)
        return {c.field_key for c in declined}

    def _budget(self, listing_id: str) -> LlmBudget:
        return LlmBudget(
            self._settings.max_llm_calls_per_listing, self._runs.total_llm_calls(listing_id)
        )

    def _new_run(self, agent: AgentName, listing_id: str, budget: LlmBudget) -> AgentRun:
        tools = ListingTools(self._conn, self._settings, self._schema, listing_id).as_mapping()
        return AgentRun(agent, listing_id, tools, self._llm, budget)

    @contextmanager
    def _agent(
        self, agent: AgentName, listing_id: str, budget: LlmBudget | None = None
    ) -> Iterator[AgentRun]:
        """Run one agent and ALWAYS persist its trace, whether it succeeds or fails."""
        run = self._new_run(agent, listing_id, budget or self._budget(listing_id))
        try:
            yield run
        except BaseException as exc:
            self._runs.save(run.to_record(error=type(exc).__name__), run.tool_calls)
            raise
        self._runs.save(run.to_record(), run.tool_calls)

    def _audit(
        self,
        action: str,
        resource_type: str,
        resource_id: str | None,
        *,
        actor_type: ActorType = ActorType.USER,
        **details,
    ) -> None:
        self._audit_log.record(
            AuditEntry(
                actor_type=actor_type,
                actor_name=LOCAL_ACTOR if actor_type is ActorType.USER else "orchestrator",
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
            )
        )
