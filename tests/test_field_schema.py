import pytest
from pydantic import ValidationError

from listing_assistant.field_schema import (
    CategorySchema,
    FieldDefinition,
    UnknownFieldError,
    load_category_schema,
)
from listing_assistant.models import Category, VisionFactProposal, new_id


@pytest.fixture(scope="module")
def car():
    return load_category_schema(Category.CAR)


def test_car_schema_loads_from_package_data(car):
    assert car.category is Category.CAR
    assert car.required_keys == ["make", "model", "model_year", "mileage_km"]


@pytest.mark.parametrize(
    "key", ["model_year", "accident_history", "painted_or_replaced_parts", "asking_price_try"]
)
def test_unobservable_fields_are_not_offered_to_vision(car, key):
    assert key not in car.photo_observable_keys


def test_vision_proposal_for_unobservable_field_is_rejected(car):
    proposal = VisionFactProposal(
        field_key="accident_history", value="kazasız", confidence=0.9, evidence_photo_id=new_id()
    )
    with pytest.raises(ValueError, match="cannot be observed"):
        car.validate_vision_proposal(proposal)


def test_vision_proposal_value_is_normalized(car):
    proposal = VisionFactProposal(
        field_key="body_type", value="SUV", confidence=0.8, evidence_photo_id=new_id()
    )
    assert car.validate_vision_proposal(proposal).value == "suv"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("120000", "120000"), ("120.000", "120000"), ("120,000", "120000"), ("1 250 000", "1250000")],
)
def test_integer_normalization(car, raw, expected):
    assert car.normalize_value("mileage_km", raw) == expected


@pytest.mark.parametrize("raw", ["1.5", "abc", "-5", "12.34.567", "3000000", ""])
def test_invalid_integers_are_rejected(car, raw):
    with pytest.raises(ValueError, match="Kilometre"):
        car.normalize_value("mileage_km", raw)


def test_choice_and_boolean_normalization(car):
    assert car.normalize_value("transmission", " Automatic ") == "automatic"
    assert car.normalize_value("sunroof", "TRUE") == "true"
    with pytest.raises(ValueError):
        car.normalize_value("transmission", "rocket")
    with pytest.raises(ValueError):
        car.normalize_value("sunroof", "belki")


def test_text_length_limit(car):
    with pytest.raises(ValueError, match="en fazla 50 karakter"):
        car.normalize_value("color", "x" * 51)


def test_unknown_field_is_rejected(car):
    with pytest.raises(UnknownFieldError):
        car.normalize_value("engine_secret", "x")


def test_schema_rejects_duplicate_keys():
    field = {
        "key": "color",
        "label_tr": "Renk",
        "value_type": "text",
        "required": False,
        "observable_from_photo": "yes",
    }
    with pytest.raises(ValidationError, match="duplicate"):
        CategorySchema.model_validate(
            {"category": "car", "version": 1, "field_definitions": [field, field]}
        )


def test_choice_field_must_define_choices():
    with pytest.raises(ValidationError, match="choices"):
        FieldDefinition(
            key="gear",
            label_tr="Vites",
            value_type="choice",
            required=False,
            observable_from_photo="partial",
        )
