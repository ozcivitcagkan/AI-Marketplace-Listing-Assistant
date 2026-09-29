"""Seller-facing value display and the final listing layout (title, specs, sections, equipment)."""

import json

import pytest

from listing_assistant.field_schema import FieldGroup, ValueType, load_category_schema
from listing_assistant.listing_format import render_listing
from listing_assistant.models import (
    Category,
    Claim,
    CopywriterOutput,
    Draft,
    Fact,
    FactSource,
    FactStatus,
    ListingSection,
    new_id,
)
from listing_assistant.policy import ABSOLUTE_CLAIM_PATTERNS

LISTING = new_id()


@pytest.fixture(scope="module")
def car():
    return load_category_schema(Category.CAR)


def fact(key, value, status=FactStatus.APPROVED):
    return Fact(
        listing_id=LISTING, field_key=key, value=value, source=FactSource.USER, status=status
    )


# --- display values --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "stored", "shown"),
    [
        ("transmission", "manual", "Manuel"),
        ("transmission", "semi_automatic", "Yarı Otomatik"),
        ("body_type", "convertible", "Cabrio"),
        ("fuel_type", "lpg", "Benzin & LPG"),
        ("sunroof", "true", "Var"),
        ("sunroof", "false", "Yok"),
        ("trade_in", "true", "Olur"),
        ("mileage_km", "222000", "222.000 km"),
        ("asking_price_try", "800000", "800.000 TL"),
        ("model_year", "2013", "2013"),  # a year is not a quantity: no "2.013"
        ("color", "siyah", "Siyah"),
        ("city", "istanbul", "İstanbul"),
    ],
)
def test_values_are_shown_in_turkish_and_capitalised(car, key, stored, shown):
    assert car.display_value(key, stored) == shown


@pytest.mark.parametrize(
    ("key", "typed", "stored"),
    [
        ("transmission", "Manuel", "manual"),
        ("transmission", "yarı otomatik", "semi_automatic"),
        ("transmission", "MANUAL", "manual"),
        ("sunroof", "Var", "true"),
        ("sunroof", "hayır", "false"),
        ("trade_in", "Olmaz", "false"),
    ],
)
def test_turkish_labels_are_accepted_as_input(car, key, typed, stored):
    assert car.normalize_value(key, typed) == stored


def test_schema_data_is_complete(car):
    for definition in car.field_definitions:
        assert isinstance(definition.group, FieldGroup)
        if definition.value_type is ValueType.CHOICE:
            assert set(definition.choice_labels) == set(definition.choices)
        # An absence term the Safety Reviewer does not know would silently never match.
        assert set(definition.absence_terms) <= set(ABSOLUTE_CLAIM_PATTERNS)


# --- the rendered listing ---------------------------------------------------------------------


def draft_with(facts_by_key, sentences):
    title = Claim(text="2013 Citroën C4 1.6 HDi", fact_ids=[facts_by_key["make"].id])
    claims = [
        Claim(text=text, fact_ids=[facts_by_key[key].id], section=section)
        for text, key, section in sentences
    ]
    return Draft(
        listing_id=LISTING,
        version=1,
        content=CopywriterOutput(title=title, sentences=claims),
        model="fake",
        prompt_version="copywriter_v2",
    )


def test_listing_has_specs_sections_in_fixed_order_and_equipment(car):
    facts = {
        "make": fact("make", "Citroën"),
        "model": fact("model", "C4"),
        "mileage_km": fact("mileage_km", "222000"),
        "transmission": fact("transmission", "manual"),
        "city": fact("city", "İstanbul"),
        "equipment": fact("equipment", "hız sabitleyici, park sensörü; çift bölgeli klima."),
        "color": fact("color", "kırmızı", status=FactStatus.REJECTED),
    }
    draft = draft_with(
        facts,
        [
            ("Aracım İstanbul'da.", "city", ListingSection.SALE),
            ("Aracım 2013 model bir Citroën C4.", "make", ListingSection.OVERVIEW),
        ],
    )
    listing = render_listing(draft, list(facts.values()), car)

    assert [(r.label, r.value) for r in listing.specs] == [
        ("Marka", "Citroën"),
        ("Model", "C4"),
        ("Kilometre", "222.000 km"),
        ("Vites Tipi", "Manuel"),
        ("Şehir", "İstanbul"),
    ]  # the rejected colour is not listed
    assert [s.heading for s in listing.sections] == ["Aracım Hakkında", "Satış Bilgileri"]
    assert listing.equipment == ("Hız sabitleyici", "Park sensörü", "Çift bölgeli klima")
    text = listing.text
    assert text.startswith("2013 Citroën C4 1.6 HDi\n\nARAÇ BİLGİLERİ\n• Marka: Citroën")
    assert text.index("ARACIM HAKKINDA") < text.index("SATIŞ BİLGİLERİ") < text.index("DONANIM")


def test_drafts_saved_before_sections_still_load():
    old = {"title": {"text": "Renault Clio", "fact_ids": [new_id()]}}
    old["sentences"] = [{"text": "Araç beyaz.", "fact_ids": [new_id()]}]
    content = CopywriterOutput.model_validate_json(json.dumps(old))
    assert content.sentences[0].section is ListingSection.OVERVIEW
