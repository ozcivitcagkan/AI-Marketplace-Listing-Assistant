"""Fact Reconciler: merges seller facts, photo proposals and note proposals (doc §4, §7.1).

- The comparison and conflict logic is plain code (`plan_proposals`, `find_conflicts`).
- The only model call reads the seller's free-text notes and proposes schema fields.
  Notes are untrusted: the model sees them inside escaped tags, and whatever it proposes
  is saved as PROPOSED only, so a manipulated note can at worst produce a suggestion the
  seller must confirm at approval gate 1.
"""

from collections import defaultdict
from dataclasses import dataclass

from listing_assistant.agent_io import NoteExtraction
from listing_assistant.field_schema import CategorySchema
from listing_assistant.llm import LLMRequest, TextPart
from listing_assistant.models import (
    Fact,
    FactSource,
    FactStatus,
    VisionFactProposal,
)
from listing_assistant.prompting import load_prompt, wrap_untrusted
from listing_assistant.text_utils import comparison_key
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import describe_field

NOTE_PROMPT = "note_extractor_v1"
# Below this, a photo proposal is not worth the seller's attention (doc §7.1, threshold).
MIN_VISION_CONFIDENCE = 0.5


@dataclass(frozen=True)
class Conflict:
    field_key: str
    candidates: tuple[Fact, ...]


@dataclass(frozen=True)
class ReconciliationReport:
    saved: tuple[Fact, ...]
    corroborated: tuple[str, ...]  # fields where a photo agreed with the seller
    low_confidence: tuple[str, ...]
    rejected_note_proposals: int
    conflicts: tuple[Conflict, ...]


def find_conflicts(facts: list[Fact]) -> list[Conflict]:
    """A conflict is a field with more than one distinct, not-rejected value."""
    by_field: dict[str, list[Fact]] = defaultdict(list)
    for fact in facts:
        if fact.status is not FactStatus.REJECTED:
            by_field[fact.field_key].append(fact)
    conflicts = []
    for key, candidates in by_field.items():
        if len({comparison_key(f.value) for f in candidates}) > 1:
            conflicts.append(Conflict(key, tuple(candidates)))
    return conflicts


def plan_proposals(
    listing_id: str,
    existing: list[Fact],
    vision: list[VisionFactProposal],
    notes: list[tuple[str, str]],
) -> tuple[list[Fact], list[str], list[str]]:
    """Decide which proposals become new PROPOSED facts. Pure function, no I/O.

    Returns (new_facts, corroborated_fields, low_confidence_fields). A value the seller
    already saw (approved, proposed or rejected) is never proposed again.
    """
    known = {(f.field_key, comparison_key(f.value)) for f in existing}
    approved = {
        (f.field_key, comparison_key(f.value)) for f in existing if f.status is FactStatus.APPROVED
    }
    corroborated: set[str] = set()
    low_confidence: set[str] = set()

    best_vision: dict[tuple[str, str], VisionFactProposal] = {}
    for proposal in vision:
        if proposal.confidence < MIN_VISION_CONFIDENCE:
            low_confidence.add(proposal.field_key)
            continue
        key = (proposal.field_key, comparison_key(proposal.value))
        if key not in best_vision or proposal.confidence > best_vision[key].confidence:
            best_vision[key] = proposal

    new_facts: list[Fact] = []
    for key, proposal in best_vision.items():
        if key in approved:
            corroborated.add(proposal.field_key)
        if key in known:
            continue
        known.add(key)
        new_facts.append(
            Fact(
                listing_id=listing_id,
                field_key=proposal.field_key,
                value=proposal.value,
                source=FactSource.VISION,
                confidence=proposal.confidence,
                evidence_photo_id=proposal.evidence_photo_id,
            )
        )

    for field_key, value in notes:
        key = (field_key, comparison_key(value))
        if key in known:
            continue
        known.add(key)
        new_facts.append(
            Fact(listing_id=listing_id, field_key=field_key, value=value, source=FactSource.USER)
        )
    return new_facts, sorted(corroborated), sorted(low_confidence - corroborated)


def extract_note_proposals(
    run: AgentRun, schema: CategorySchema, notes: str
) -> tuple[list[tuple[str, str]], int]:
    """Ask the model for explicit statements in the notes; keep only valid schema values."""
    if not notes.strip():
        return [], 0
    prompt = load_prompt(NOTE_PROMPT)
    fields = "\n".join(describe_field(d) for d in schema.field_definitions)
    request = LLMRequest(
        system=prompt.text,
        parts=(TextPart(f"Allowed fields:\n{fields}\n\n{wrap_untrusted('seller_notes', notes)}"),),
        output_model=NoteExtraction,
        prompt_version=prompt.version,
    )
    extraction: NoteExtraction = run.generate(request)
    accepted, rejected = [], 0
    for proposal in extraction.proposals:
        try:
            accepted.append(
                (proposal.field_key, schema.normalize_value(proposal.field_key, proposal.value))
            )
        except ValueError:  # includes unknown fields
            rejected += 1
    return accepted, rejected


def run_fact_reconciler(
    run: AgentRun,
    schema: CategorySchema,
    vision: list[VisionFactProposal],
    notes: str,
) -> ReconciliationReport:
    existing: list[Fact] = run.call("get_listing_facts", status=None)
    note_proposals, rejected_notes = extract_note_proposals(run, schema, notes)
    new_facts, corroborated, low_confidence = plan_proposals(
        run.listing_id, existing, vision, note_proposals
    )
    saved = run.call("save_fact_proposals", facts=new_facts) if new_facts else []
    all_facts: list[Fact] = run.call("get_listing_facts", status=None)
    return ReconciliationReport(
        saved=tuple(saved),
        corroborated=tuple(corroborated),
        low_confidence=tuple(low_confidence),
        rejected_note_proposals=rejected_notes,
        conflicts=tuple(find_conflicts(all_facts)),
    )
