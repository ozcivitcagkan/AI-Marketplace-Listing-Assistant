"""The listing policy: our own synthetic writing rules (architecture doc §9.2).

Kept as code-level data so the Copywriter's prompt, the Safety Reviewer's prompt and
the deterministic safety checks all read the same lists. Not copied from any platform.
"""

import re

from listing_assistant.text_utils import tr_lower

# Absolute / absence claims. Allowed only when a seller-sourced fact states them, and
# even then flagged with a warning (architecture doc §7.1 "Mutlak iddia kuralı").
ABSOLUTE_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    term: re.compile(pattern)
    for term, pattern in {
        "hatasız": r"\bhatasız",
        "boyasız": r"\bboyasız",
        "tramersiz": r"\btramersiz",
        "kazasız": r"\bkazasız",
        "hasarsız": r"\bhasarsız",
        "değişensiz": r"\bdeğişensiz",
        "sorunsuz": r"\bsorunsuz",
        "kusursuz": r"\bkusursuz",
        "hasar yok": r"\bhasar\w*\s+(yok|bulunma)",
        "kaza yok": r"\bkaza\w*\s+(yok|bulunma|yapma)",
        "boya yok": r"\bboya\w*\s+(yok|bulunma)",
        "sıfır ayarında": r"\bsıfır\s+ayarında",
    }.items()
}

# A seller's answer that states absence for a field ("görünür hasar: yok").
NEGATIVE_ANSWERS = frozenset(
    {"yok", "yoktur", "hayır", "false", "bulunmuyor", "bulunmamaktadır", "olmadı", "olmamıştır"}
)

# Exaggerated marketing language: a warning, not a blocker.
HYPE_TERMS = ("muhteşem", "kaçırılmaz", "eşsiz", "mükemmel", "efsane", "kelepir", "fırsat")


def find_absolute_terms(text: str) -> list[str]:
    lowered = tr_lower(text)
    return [term for term, pattern in ABSOLUTE_CLAIM_PATTERNS.items() if pattern.search(lowered)]


def find_hype_terms(text: str) -> list[str]:
    lowered = tr_lower(text)
    return [term for term in HYPE_TERMS if term in lowered]


def style_rules_text() -> str:
    return "\n".join(
        [
            "Listing policy (synthetic, owned by this project):",
            "- Natural, factual Turkish in the seller's own first-person voice. One topic per"
            " sentence.",
            "- Absolute or absence claims are forbidden unless a seller-sourced fact states"
            " them: " + ", ".join(ABSOLUTE_CLAIM_PATTERNS) + ".",
            "- No invented experiences, feelings or promises (how the car was used or kept,"
            " why it is sold, guarantees) unless a cited fact states them.",
            "- Avoid marketing superlatives such as: " + ", ".join(HYPE_TERMS) + ".",
            "- No personal data: phone numbers, e-mail addresses, national ID numbers, IBANs,"
            " licence plates, chassis numbers.",
            "- No discriminatory statements about buyers.",
            "- Photo-sourced facts describe what was visible in the photos only.",
        ]
    )
