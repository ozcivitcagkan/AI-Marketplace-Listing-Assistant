"""Quality & Safety Reviewer: the output guardrail before human approval (doc §4, §7).

Two independent passes, and the final decision is always derived by code:
1. Code checks (deterministic): cited facts, personal data, numbers not found in the
   cited facts, absolute/absence claims, hype words, injection patterns; plus personal
   data, hype words and injection patterns in the fact values code prints in the export.
2. A model review with a different prompt and task than the Copywriter ("second pair of eyes").
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
from listing_assistant.listing_format import code_rendered_values
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
    # The pattern name is an internal identifier: the audit log keeps it, the seller does not.
    return "Talimat gibi görünen bir ifade var."


def fact_pii_message(label: str, kind: PiiKind, masked: str) -> str:
    return f"'{label}' bilgisi kişisel veri içeriyor ({PII_LABELS_TR[kind]}: {masked})."


def fact_hype_message(label: str, term: str) -> str:
    return f"'{label}' bilgisinde abartılı ifade var: '{term}'."


def fact_injection_message(label: str) -> str:
    return f"'{label}' bilgisinde talimat gibi görünen bir ifade var."


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


# Words that report something present or limit a negation ("başka hasar yok").
_PRESENCE = re.compile(
    r"\b(var|vardı|vardır|mevcut\w*|başka|dışında|hariç|haricinde|sadece|yalnızca|ama|fakat"
    r"|ancak)\b"
)
_CLAUSE_BREAK = re.compile(r"[,;\n]|\.\s|\bve\b")


def seller_stated(term: str, fact: Fact, schema: CategorySchema) -> bool:
    """Did the seller assert this absolute term in this fact?

    Read as the seller's statement "label value", so "Görünür hasar" + "yok" states
    "hasar yok". A plain negative answer also supports the field's own absence terms
    ("Boyalı / değişen parça: yok" supports "boyasız"). Photo facts never qualify:
    absence cannot be observed.
    """
    if fact.source is not FactSource.USER:
        return False
    definition = schema.get(fact.field_key)
    if term in definition.absence_terms and comparison_key(fact.value) in NEGATIVE_ANSWERS:
        return True
    # A partial negation ("Sol kapıda göçük var, başka hasar yok") must not back a blanket
    # claim, so every clause has to state absence and nothing may be reported present.
    value = tr_lower(fact.value)
    if _PRESENCE.search(value):
        return False
    clauses = [c for c in _CLAUSE_BREAK.split(value) if c.strip()]
    statements = [f"{tr_lower(definition.label_tr)} {c.strip()}" for c in clauses]
    if not all(
        find_absolute_terms(s) or comparison_key(c) in NEGATIVE_ANSWERS
        for s, c in zip(statements, clauses, strict=True)
    ):
        return False
    return any(ABSOLUTE_CLAIM_PATTERNS[term].search(s) for s in statements)


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
    issues += review_rendered_facts(run, list(facts_by_id.values()), schema)
    return SafetyVerdict(draft_id=draft.id, reviewer_type=ReviewerType.CODE, issues=issues)


def review_rendered_facts(
    run: AgentRun, facts: list[Fact], schema: CategorySchema
) -> list[SafetyIssue]:
    """Check the fact values code prints in the export (spec list, equipment list).

    Only the seller can change these values, so the issues are not sent back to the
    Copywriter (fixable=False) and have no claim index.
    """
    issues: list[SafetyIssue] = []

    def add(kind: IssueType, severity: IssueSeverity, message: str) -> None:
        issues.append(
            SafetyIssue(issue_type=kind, severity=severity, message=message, fixable=False)
        )

    for row in code_rendered_values(facts, schema):
        matches: list[PiiMatch] = run.call("pii_scan_text", text=row.value)
        for match in matches:
            severity = IssueSeverity.BLOCK if match.blocking else IssueSeverity.WARN
            add(
                IssueType.SENSITIVE_INFO,
                severity,
                fact_pii_message(row.label, match.kind, match.masked),
            )
        for term in find_hype_terms(row.value):
            add(IssueType.PROHIBITED_PHRASE, IssueSeverity.WARN, fact_hype_message(row.label, term))
        if find_injection_patterns(row.value):
            add(IssueType.PROMPT_INJECTION, IssueSeverity.BLOCK, fact_injection_message(row.label))
    return issues


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
