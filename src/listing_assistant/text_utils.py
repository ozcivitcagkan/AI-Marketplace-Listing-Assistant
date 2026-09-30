"""Small text helpers that are Turkish-aware."""

import re


def tr_lower(text: str) -> str:
    """Lower-case with Turkish rules: 'I' -> 'ı' and 'İ' -> 'i' (str.lower gets both wrong)."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def tr_upper(text: str) -> str:
    """Upper-case with Turkish rules: 'i' -> 'İ' and 'ı' -> 'I'."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def tr_capitalize(text: str) -> str:
    """Upper-case the first letter only: 'siyah kumaş' -> 'Siyah kumaş', 'izmir' -> 'İzmir'."""
    return tr_upper(text[:1]) + text[1:]


def group_thousands(number: int) -> str:
    """Turkish digit grouping: 222000 -> '222.000'."""
    return f"{number:,}".replace(",", ".")


def comparison_key(value: str) -> str:
    """Normalise a value for equality checks: 'Kırmızı ' and 'kırmızı' are the same value."""
    return re.sub(r"\s+", " ", tr_lower(value)).strip()


def place_key(value: str) -> str:
    """Like comparison_key, but 'Istanbul', 'İstanbul' and 'istanbul' are the same place.

    Sellers often type Turkish place names without the dotted capital, so dotted and
    dotless i are folded together. Kept separate so answers like "hayır" are unaffected.
    """
    return comparison_key(value).replace("ı", "i")
