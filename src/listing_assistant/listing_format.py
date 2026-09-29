"""Assembles the final listing text the seller copies to a marketplace.

The layout follows common Turkish second-hand car listings: a title, a spec list, headed
description sections and an equipment list. Only two kinds of content appear:
- the Copywriter's sentences (each already traced to approved facts), and
- approved fact values rendered by code (spec list, equipment list).
So a longer, richer listing still contains no sentence without a source.
"""

import re
from dataclasses import dataclass

from listing_assistant.field_schema import CategorySchema
from listing_assistant.models import Draft, Fact, FactStatus, ListingSection
from listing_assistant.text_utils import tr_capitalize, tr_upper

SECTION_HEADINGS: dict[ListingSection, str] = {
    ListingSection.OVERVIEW: "Aracım Hakkında",
    ListingSection.APPEARANCE: "Dış ve İç Görünüm",
    ListingSection.CONDITION: "Hasar, Boya ve Tramer Durumu",
    ListingSection.MAINTENANCE: "Motor, Bakım ve Mekanik",
    ListingSection.SALE: "Satış Bilgileri",
}
SPEC_HEADING = "Araç Bilgileri"
EQUIPMENT_HEADING = "Donanım"
EQUIPMENT_KEY = "equipment"
BULLET = "• "


@dataclass(frozen=True)
class SpecRow:
    label: str
    value: str


@dataclass(frozen=True)
class Section:
    heading: str
    sentences: tuple[str, ...]


@dataclass(frozen=True)
class RenderedListing:
    title: str
    specs: tuple[SpecRow, ...]
    sections: tuple[Section, ...]
    equipment: tuple[str, ...]

    @property
    def description(self) -> str:
        """Everything below the title, as plain text that pastes cleanly anywhere."""
        blocks: list[str] = []
        if self.specs:
            rows = "\n".join(f"{BULLET}{row.label}: {row.value}" for row in self.specs)
            blocks.append(f"{tr_upper(SPEC_HEADING)}\n{rows}")
        for section in self.sections:
            blocks.append(f"{tr_upper(section.heading)}\n{' '.join(section.sentences)}")
        if self.equipment:
            items = "\n".join(f"{BULLET}{item}" for item in self.equipment)
            blocks.append(f"{tr_upper(EQUIPMENT_HEADING)}\n{items}")
        return "\n\n".join(blocks)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.description}\n"


def spec_rows(facts: list[Fact], schema: CategorySchema) -> list[SpecRow]:
    approved = {f.field_key: f for f in facts if f.status is FactStatus.APPROVED}
    return [
        SpecRow(d.label_tr, d.display_value(approved[d.key].value))
        for d in schema.field_definitions
        if d.in_spec_table and d.key in approved
    ]


def equipment_items(facts: list[Fact]) -> list[str]:
    """'hız sabitleyici, park sensörü' -> ['Hız sabitleyici', 'Park sensörü']."""
    for fact in facts:
        if fact.field_key == EQUIPMENT_KEY and fact.status is FactStatus.APPROVED:
            parts = (p.strip(" .") for p in re.split(r"[,;\n]", fact.value))
            return [tr_capitalize(p) for p in parts if p]
    return []


def render_listing(draft: Draft, facts: list[Fact], schema: CategorySchema) -> RenderedListing:
    sections = []
    for section, heading in SECTION_HEADINGS.items():
        sentences = tuple(c.text for c in draft.content.sentences if c.section is section)
        if sentences:
            sections.append(Section(heading, sentences))
    return RenderedListing(
        title=draft.title,
        specs=tuple(spec_rows(facts, schema)),
        sections=tuple(sections),
        equipment=tuple(equipment_items(facts)),
    )
