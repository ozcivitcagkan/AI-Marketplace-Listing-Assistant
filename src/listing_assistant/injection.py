"""Pattern scan for prompt-injection attempts in untrusted text (architecture doc §7.2 #4).

This is a tripwire, not the defence: a match is flagged, logged and shown to the seller.
The real protection is structural (tags, schemas, least privilege, human approval), so
an attack that slips past these patterns still cannot do more than propose a value.
"""

import re

from listing_assistant.text_utils import tr_lower

INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "ignore_instructions_en": (
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all"
            r"|system)\b.{0,40}\b(instructions?|prompts?|rules)\b"
        ),
        "ignore_instructions_tr": (
            r"(önceki|yukarıdaki|tüm|bütün)\s+(talimat|komut|kural)\w*\s*"
            r"(yok\s*say|unut|görmezden\s*gel|dikkate\s*alma)"
        ),
        "system_prompt_reference": r"\b(system|sistem)\s*(prompt|istemi|mesajı|talimatı)",
        "role_marker": r"(?m)^\s*(system|sistem|assistant|asistan|developer)\s*:",
        "role_override_en": r"\b(you are now|act as|new instructions|developer mode)\b",
        "role_override_tr": r"\b(artık sen|yeni talimat|geliştirici modu)",
        "tag_breakout": r"</?\s*(seller_notes|approved_facts|draft_claims|previous_draft"
        r"|reviewer_feedback|seller_change_request|system)\s*>",
    }.items()
}


def find_injection_patterns(text: str) -> list[str]:
    lowered = tr_lower(text)
    return [name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(lowered)]
