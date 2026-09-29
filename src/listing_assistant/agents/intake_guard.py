"""Intake Guard: checks every piece of seller-typed text before it is stored (doc §4, §7.5).

Pure code, no model. National ID numbers and IBANs are refused outright; phone numbers
and e-mail addresses need the seller's explicit confirmation; plates and chassis
numbers produce a warning. Injection-like wording is allowed (it is the seller's own
text, and it is only ever treated as data) but flagged so it can be logged and shown.
"""

from dataclasses import dataclass

from listing_assistant.injection import find_injection_patterns
from listing_assistant.pii import CONTACT_KINDS, PII_LABELS_TR, PiiMatch
from listing_assistant.tools import AgentRun


class InputRejectedError(ValueError):
    """Seller input that must not be stored. The message never repeats the raw value."""


class ConfirmationRequiredError(InputRejectedError):
    """Contact details were found; the seller must confirm adding them knowingly."""


@dataclass(frozen=True)
class IntakeFindings:
    warnings: tuple[str, ...] = ()
    injection_patterns: tuple[str, ...] = ()


def check_user_text(run: AgentRun, text: str, *, contact_confirmed: bool) -> IntakeFindings:
    if not text.strip():
        return IntakeFindings()
    matches: list[PiiMatch] = run.call("pii_scan_text", text=text)
    blocking = [m for m in matches if m.blocking]
    if blocking:
        kinds = ", ".join(sorted({f"{PII_LABELS_TR[m.kind]} ({m.masked})" for m in blocking}))
        raise InputRejectedError(f"Bu bilgi ilana eklenemez: {kinds}.")
    contact = [m for m in matches if m.kind in CONTACT_KINDS]
    if contact and not contact_confirmed:
        raise ConfirmationRequiredError(
            "İletişim bilgisi bulundu ("
            + ", ".join(sorted({PII_LABELS_TR[m.kind] for m in contact}))
            + "). Bilerek eklemek istiyorsanız onay kutusunu işaretleyin."
        )
    return IntakeFindings(
        warnings=tuple(f"{PII_LABELS_TR[m.kind]} ({m.masked})" for m in matches),
        injection_patterns=tuple(find_injection_patterns(text)),
    )
