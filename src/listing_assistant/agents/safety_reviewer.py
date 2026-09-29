"""Quality & Safety Reviewer: the output guardrail before human approval (doc §4, §7).

Two independent passes, and the final decision is always derived by code:
1. Code checks (deterministic): cited facts, personal data, numbers not found in the
   cited facts, absolute/absence claims, hype words, injection patterns.
2. A model review with a different prompt and task than the Copywriter ("second eye").
   It can only ADD issues; it cannot clear anything the code found. It is skipped when
   the code already blocks the draft, to save cost.

Issue messages are shown to the seller and fed back to the Copywriter, so they are Turkish.
"""

import json
import re

from listing_assistant.agent_io import SafetyFindings
from listing_assistant.claims import unsupported_numbers
from listing_assistant.field_schema import CategorySchema
from listing_assistant.injection import find_injection_patterns
from listing_assistant.llm import LLMRequest, TextPart
from listing_assistant.models import (
    Draft,
    Fact,
    FactSource,
    IssueSeverity,
    IssueType,
    ReviewerType,
    SafetyDecision,
    SafetyIssue,
    SafetyVerdict,
)
from listing_assistant.pii import PII_LABELS_TR, PiiKind, PiiMatch
from listing_assistant.policy import (
    ABSOLUTE_CLAIM_PATTERNS,
    NEGATIVE_ANSWERS,
    find_absolute_terms,
    find_hype_terms,
)
from listing_assistant.prompting import load_prompt, wrap_untrusted
from listing_assistant.text_utils import comparison_key, tr_lower
from listing_assistant.tools import AgentRun

SAFETY_PROMPT = "safety_reviewer_v2"


def _issue(kind, severity, message, index) -> SafetyIssue:
    return SafetyIssue(issue_type=kind, severity=severity, message=message, claim_index=index)


# --- seller-facing messages (Turkish) ---------------------------------------------------------

UNAPPROVED_SOURCE = "Bu cümle onaylanmamış bir bilgiye dayanıyor."


def pii_message(kind: PiiKind, masked: str) -> str:
    return f"Kişisel veri içeriyor ({PII_LABELS_TR[kind]}: {masked})."


def numbers_message(numbers: str) -> str:
    return f"Onaylı bilgilerde geçmeyen sayı: {numbers}."


def absolute_warning(term: str) -> str:
    return (
        f"'{term}' kesin bir ifade. Bu sizin kendi beyanınız; alıcılar ekspertizle"
        " doğrulamak isteyebilir."
    )


def absolute_block(term: str) -> str:
    return (
        f"'{term}' kesin bir ifade, ama girdiğiniz bilgilerde böyle bir beyan yok."
        " Fotoğraflar bir şeyin olmadığını kanıtlayamaz."
    )


def hype_message(term: str) -> str:
    return f"Abartılı ifade: '{term}'."


def injection_message(pattern_name: str) -> str:
    return f"Talimat gibi görünen metin var ({pattern_name})."


# Reviews are append-only, so ones stored before messages became Turkish keep their English
# text. These patterns show such stored code-review messages in Turkish as well.
_LEGACY_MESSAGES = [
    (re.compile(r"Claim cites facts that are not approved\."), lambda m: UNAPPROVED_SOURCE),
    (
        re.compile(r"Personal data \((\w+), (.+)\)\."),
        lambda m: pii_message(PiiKind(m[1]), m[2]),
    ),
    (re.compile(r"Numbers not found in the cited facts: (.+)\."), lambda m: numbers_message(m[1])),
    (
        re.compile(r"Absolute claim '(.+)' is the seller's own statement; .*"),
        lambda m: absolute_warning(m[1]),
    ),
    (
        re.compile(r"Absolute claim '(.+)' is not stated by the seller\."),
        lambda m: absolute_block(m[1]),
    ),
    (re.compile(r"Exaggerated wording '(.+)'\."), lambda m: hype_message(m[1])),
    (re.compile(r"Instruction-like text \((.+)\)\."), lambda m: injection_message(m[1])),
]


def seller_message(message: str) -> str:
    """The Turkish text to show the seller for a stored issue message."""
    for pattern, render in _LEGACY_MESSAGES:
        if match := pattern.fullmatch(message):
            try:
                return render(match)
            except (KeyError, ValueError):
                break
    return message


def seller_stated(term: str, fact: Fact, schema: CategorySchema) -> bool:
    """Did the seller themself assert this absolute term in this fact?

    Read as the seller's statement "label value", so "Görünür hasar" + "yok" states
    "hasar yok". A plain negative answer also supports the field's own absence terms
    ("Boyalı / değişen parça: yok" supports "boyasız"). Photo facts never qualify:
    absence cannot be observed.
    """
    if fact.source is not FactSource.USER:
        return False
    definition = schema.get(fact.field_key)
    statement = tr_lower(f"{definition.label_tr} {fact.value}")
    if ABSOLUTE_CLAIM_PATTERNS[term].search(statement):
        return True
    return term in definition.absence_terms and comparison_key(fact.value) in NEGATIVE_ANSWERS


def review_by_code(
    run: AgentRun, draft: Draft, facts_by_id: dict[str, Fact], schema: CategorySchema
) -> SafetyVerdict:
    issues: list[SafetyIssue] = []
    for index, claim in enumerate(draft.all_claims):
        cited = [facts_by_id[fid] for fid in claim.fact_ids if fid in facts_by_id]
        if len(cited) != len(claim.fact_ids):
            issues.append(
                _issue(
                    IssueType.UNSUPPORTED_CLAIM,
                    IssueSeverity.BLOCK,
                    UNAPPROVED_SOURCE,
                    index,
                )
            )

        matches: list[PiiMatch] = run.call("pii_scan_text", text=claim.text)
        for match in matches:
            severity = IssueSeverity.BLOCK if match.blocking else IssueSeverity.WARN
            issues.append(
                _issue(
                    IssueType.SENSITIVE_INFO,
                    severity,
                    pii_message(match.kind, match.masked),
                    index,
                )
            )

        numbers = unsupported_numbers(claim.text, [f.value for f in cited])
        if numbers:
            issues.append(
                _issue(
                    IssueType.UNSUPPORTED_CLAIM,
                    IssueSeverity.BLOCK,
                    numbers_message(", ".join(sorted(numbers))),
                    index,
                )
            )

        for term in find_absolute_terms(claim.text):
            if any(seller_stated(term, f, schema) for f in cited):
                issues.append(
                    _issue(
                        IssueType.PROHIBITED_PHRASE,
                        IssueSeverity.WARN,
                        absolute_warning(term),
                        index,
                    )
                )
            else:
                issues.append(
                    _issue(
                        IssueType.PROHIBITED_PHRASE,
                        IssueSeverity.BLOCK,
                        absolute_block(term),
                        index,
                    )
                )

        for term in find_hype_terms(claim.text):
            issues.append(
                _issue(
                    IssueType.PROHIBITED_PHRASE,
                    IssueSeverity.WARN,
                    hype_message(term),
                    index,
                )
            )

        for pattern_name in find_injection_patterns(claim.text):
            issues.append(
                _issue(
                    IssueType.PROMPT_INJECTION,
                    IssueSeverity.BLOCK,
                    injection_message(pattern_name),
                    index,
                )
            )
    return SafetyVerdict(draft_id=draft.id, reviewer_type=ReviewerType.CODE, issues=issues)


def review_by_model(
    run: AgentRun, draft: Draft, facts: list[Fact], schema: CategorySchema
) -> SafetyVerdict:
    claims = [
        {"index": i, "text": claim.text, "fact_ids": claim.fact_ids}
        for i, claim in enumerate(draft.all_claims)
    ]
    fact_rows = [
        {
            "fact_id": f.id,
            "label": schema.get(f.field_key).label_tr,
            "value": schema.display_value(f.field_key, f.value),
            "source": "seller" if f.source is FactSource.USER else f.source.value,
        }
        for f in facts
    ]
    style_rules: str = run.call("get_style_rules")
    prompt = load_prompt(SAFETY_PROMPT)
    findings: SafetyFindings = run.generate(
        LLMRequest(
            system=prompt.text,
            parts=(
                TextPart(
                    "\n\n".join(
                        [
                            style_rules,
                            wrap_untrusted(
                                "approved_facts", json.dumps(fact_rows, ensure_ascii=False)
                            ),
                            wrap_untrusted("draft_claims", json.dumps(claims, ensure_ascii=False)),
                        ]
                    )
                ),
            ),
            output_model=SafetyFindings,
            prompt_version=prompt.version,
        )
    )
    count = len(claims)
    issues = [
        SafetyIssue(
            issue_type=f.issue_type,
            severity=f.severity,
            message=f.message,
            # An index the draft does not have is kept as a general issue, not dropped.
            claim_index=f.claim_index
            if f.claim_index is not None and f.claim_index < count
            else None,
        )
        for f in findings.findings
    ]
    return SafetyVerdict(draft_id=draft.id, reviewer_type=ReviewerType.LLM, issues=issues)


def run_safety_reviewer(run: AgentRun, draft: Draft, schema: CategorySchema) -> list[SafetyVerdict]:
    if draft.listing_id != run.listing_id:
        raise PermissionError("draft belongs to a different listing")
    facts: list[Fact] = run.call("get_listing_facts")
    code_verdict = review_by_code(run, draft, {f.id: f for f in facts}, schema)
    if code_verdict.decision is SafetyDecision.BLOCK:
        return [code_verdict]
    return [code_verdict, review_by_model(run, draft, facts, schema)]


def combined_decision(verdicts: list[SafetyVerdict]) -> SafetyDecision:
    decisions = {v.decision for v in verdicts}
    for decision in (SafetyDecision.BLOCK, SafetyDecision.WARN):
        if decision in decisions:
            return decision
    return SafetyDecision.PASS
