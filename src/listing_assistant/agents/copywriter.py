"""Copywriter: writes the Turkish title and sentences from APPROVED facts only (doc §4, §7.1).

What it sees: approved facts (id, field, value, source), the style rules, the previous
draft it is asked to fix, reviewer feedback and the seller's change request. What it
never sees: photos, seller notes, proposed or rejected facts. Its output is a
`CopywriterOutput` whose every sentence cites fact IDs; `save_draft` refuses the draft if
any cited ID is not an approved fact.
"""

import json
from dataclasses import dataclass

from listing_assistant.claims import UnsupportedClaimError
from listing_assistant.field_schema import CategorySchema
from listing_assistant.llm import LLMRequest, TextPart
from listing_assistant.models import CopywriterOutput, Draft, Fact, FactSource
from listing_assistant.prompting import load_prompt, wrap_untrusted
from listing_assistant.tools import AgentRun

COPYWRITER_PROMPT = "copywriter_v3"
_SOURCE_LABELS = {FactSource.USER: "seller", FactSource.VISION: "photo"}


@dataclass(frozen=True)
class DraftAttempt:
    draft: Draft | None
    problems: tuple[str, ...] = ()


def facts_payload(facts: list[Fact], schema: CategorySchema) -> str:
    rows = [
        {
            "fact_id": fact.id,
            "field": fact.field_key,
            "label": schema.get(fact.field_key).label_tr,
            # The display form ("Manuel", "222.000 km") is what the listing should say.
            "value": schema.display_value(fact.field_key, fact.value),
            "source": _SOURCE_LABELS.get(fact.source, fact.source.value),
        }
        for fact in facts
    ]
    return json.dumps(rows, ensure_ascii=False, indent=1)


def draft_payload(draft: Draft) -> str:
    """The earlier version with the claim indexes that feedback ("Claim 3: ...") refers to."""
    rows = [
        {
            "claim_index": index,
            "part": "title" if index == 0 else "sentence",
            "section": None if index == 0 else claim.section.value,
            "text": claim.text,
            "fact_ids": claim.fact_ids,
        }
        for index, claim in enumerate(draft.all_claims)
    ]
    return json.dumps(rows, ensure_ascii=False, indent=1)


def run_copywriter(
    run: AgentRun,
    schema: CategorySchema,
    feedback: list[str] | None = None,
    change_request: str | None = None,
    previous_draft: Draft | None = None,
) -> DraftAttempt:
    if previous_draft is not None and previous_draft.listing_id != run.listing_id:
        raise PermissionError("draft belongs to a different listing")
    facts: list[Fact] = run.call("get_listing_facts")
    if not facts:
        raise ValueError("there are no approved facts to write about")
    style_rules: str = run.call("get_style_rules")

    sections = [style_rules, wrap_untrusted("approved_facts", facts_payload(facts, schema))]
    if previous_draft is not None:
        sections.append(wrap_untrusted("previous_draft", draft_payload(previous_draft)))
    if feedback:
        sections.append(wrap_untrusted("reviewer_feedback", "\n".join(f"- {f}" for f in feedback)))
    if change_request:
        sections.append(wrap_untrusted("seller_change_request", change_request))

    prompt = load_prompt(COPYWRITER_PROMPT)
    content: CopywriterOutput = run.generate(
        LLMRequest(
            system=prompt.text,
            parts=(TextPart("\n\n".join(sections)),),
            output_model=CopywriterOutput,
            prompt_version=prompt.version,
        )
    )
    try:
        return DraftAttempt(draft=run.call("save_draft", content=content))
    except UnsupportedClaimError as exc:
        run.mark_failed("draft cited facts that are not approved")
        return DraftAttempt(
            draft=None,
            problems=(
                "Some sentences cited fact ids that are not in the approved facts: "
                + ", ".join(sorted(exc.unknown_ids))
                + ". Cite only the fact_id values given in approved_facts.",
            ),
        )
