"""Personal data detection in text: regex candidates confirmed by validators (doc §7.5).

National ID (TC Kimlik No) and IBAN candidates must pass their checksum algorithms,
which removes most false positives. Policy: national ID and IBAN block; everything else
is a warning the seller must knowingly accept.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class PiiKind(StrEnum):
    NATIONAL_ID = "national_id"
    IBAN = "iban"
    PHONE = "phone"
    EMAIL = "email"
    LICENSE_PLATE = "license_plate"
    VIN = "vin"


BLOCKING_KINDS = frozenset({PiiKind.NATIONAL_ID, PiiKind.IBAN})
CONTACT_KINDS = frozenset({PiiKind.PHONE, PiiKind.EMAIL})
# Seller-facing names of each kind.
PII_LABELS_TR = {
    PiiKind.NATIONAL_ID: "TC kimlik no",
    PiiKind.IBAN: "IBAN",
    PiiKind.PHONE: "telefon",
    PiiKind.EMAIL: "e-posta",
    PiiKind.LICENSE_PLATE: "plaka",
    PiiKind.VIN: "şasi no",
}


@dataclass(frozen=True)
class PiiMatch:
    kind: PiiKind
    masked: str  # safe to log or show: only the last two characters remain
    start: int
    end: int

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_KINDS


_NATIONAL_ID = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
# Numeric IBANs (Turkey and most of Europe's numeric formats), optionally in groups of 4.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?\d{4}){3,7}(?: ?\d{1,4})?(?!\d)")
_PHONE = re.compile(
    r"(?<![\d+])(?:\+90[\s-]?|0)?\(?5\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PLATE = re.compile(r"(?<!\w)(?:0[1-9]|[1-7]\d|8[01]) ?[A-Z]{1,3} ?\d{2,4}(?!\w)")
_VIN = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")


def is_valid_national_id(value: str) -> bool:
    if len(value) != 11 or not value.isdigit() or value[0] == "0":
        return False
    d = [int(c) for c in value]
    odd_sum = d[0] + d[2] + d[4] + d[6] + d[8]
    even_sum = d[1] + d[3] + d[5] + d[7]
    return (odd_sum * 7 - even_sum) % 10 == d[9] and sum(d[:10]) % 10 == d[10]


def is_valid_iban(value: str) -> bool:
    compact = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return False
    if compact.startswith("TR") and len(compact) != 26:
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def mask(value: str) -> str:
    compact = re.sub(r"\s", "", value)
    return "*" * max(0, len(compact) - 2) + compact[-2:]


def scan_text(text: str) -> list[PiiMatch]:
    matches: list[PiiMatch] = []

    def add(kind: PiiKind, match: re.Match[str]) -> None:
        matches.append(PiiMatch(kind, mask(match.group()), match.start(), match.end()))

    for m in _NATIONAL_ID.finditer(text):
        if is_valid_national_id(m.group()):
            add(PiiKind.NATIONAL_ID, m)
    for m in _IBAN.finditer(text):
        if is_valid_iban(m.group()):
            add(PiiKind.IBAN, m)
    for m in _PHONE.finditer(text):
        add(PiiKind.PHONE, m)
    for m in _EMAIL.finditer(text):
        add(PiiKind.EMAIL, m)
    for m in _PLATE.finditer(text):
        add(PiiKind.LICENSE_PLATE, m)
    for m in _VIN.finditer(text):
        if re.search(r"\d", m.group()) and re.search(r"[A-Z]", m.group()):
            add(PiiKind.VIN, m)
    return sorted(matches, key=lambda m: m.start)
