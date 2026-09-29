"""Versioned prompt files and the rule for putting untrusted text into a prompt."""

import html
from dataclasses import dataclass
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class Prompt:
    version: str  # the file stem, e.g. "copywriter_v1"; stored with every draft and run
    text: str


@cache
def load_prompt(version: str) -> Prompt:
    path = resources.files("listing_assistant") / "prompts" / f"{version}.md"
    return Prompt(version=version, text=path.read_text(encoding="utf-8").strip())


def wrap_untrusted(tag: str, text: str) -> str:
    """Put data inside tags. Escaping <, > and & means the data cannot close the tag
    early and smuggle text outside it (architecture doc §7.2, channel separation)."""
    return f"<{tag}>\n{html.escape(text, quote=False)}\n</{tag}>"
