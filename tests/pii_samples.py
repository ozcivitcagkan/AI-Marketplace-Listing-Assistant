"""Fake identifiers that only satisfy format and checksum rules. Never real people's data."""


def fake_national_id(first_nine: str = "100000001") -> str:
    d = [int(c) for c in first_nine]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    eleventh = (sum(d) + tenth) % 10
    return f"{first_nine}{tenth}{eleventh}"


def fake_iban(bban: str = "0000100000000000000001") -> str:
    """A Turkish-format IBAN (TR + 2 check digits + 22 digits) with a valid checksum."""
    numeric = "".join(str(int(ch, 36)) for ch in bban + "TR00")
    check = 98 - int(numeric) % 97
    return f"TR{check:02d}{bban}"


def grouped(iban: str) -> str:
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
