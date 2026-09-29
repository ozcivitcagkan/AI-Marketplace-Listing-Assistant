"""Claim-to-fact traceability checks: pure code, no model involved (architecture doc §7.1).

- `unknown_fact_ids`: every cited ID must be an APPROVED fact of THIS listing.
- `unsupported_numbers`: every number in a claim must appear in the facts it cites,
  because numbers are computed or supplied, never invented by the model.
"""

import re

from listing_assistant.models import CopywriterOutput

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class UnsupportedClaimError(ValueError):
    def __init__(self, unknown_ids: set[str]) -> None:
        super().__init__(f"{len(unknown_ids)} cited fact id(s) are not approved facts")
        self.unknown_ids = unknown_ids


def unknown_fact_ids(content: CopywriterOutput, approved_ids: set[str]) -> set[str]:
    cited = {fid for claim in [content.title, *content.sentences] for fid in claim.fact_ids}
    return cited - approved_ids


def numbers_in(text: str) -> set[str]:
    """'120.000 km, 2018' -> {'120000', '2018'}: separators removed for comparison."""
    return {re.sub(r"[.,]", "", match) for match in _NUMBER.findall(text)}


def unsupported_numbers(claim_text: str, cited_values: list[str]) -> set[str]:
    supported = set().union(*(numbers_in(value) for value in cited_values))
    return numbers_in(claim_text) - supported
