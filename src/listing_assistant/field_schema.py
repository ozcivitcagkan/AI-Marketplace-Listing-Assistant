"""Category field definitions loaded from data files (architecture doc §8, §10).

Adding a category means adding a JSON file, not new code. Stored values are canonical
(choice keys such as "manual", "true"/"false", plain digits); `display_value` turns them
into what the seller reads ("Manuel", "Var", "222.000 km").

Validation messages are shown to the seller, so they are Turkish and name the field by
its Turkish label.
"""

import re
from enum import StrEnum
from functools import cache
from importlib import resources

from pydantic import Field, model_validator

from listing_assistant.models import Category, FieldKey, StrictModel, VisionFactProposal
from listing_assistant.text_utils import comparison_key, group_thousands, tr_capitalize


class ValueType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    CHOICE = "choice"


class PhotoObservability(StrEnum):
    YES = "yes"
    PARTIAL = "partial"
    NO = "no"


class FieldGroup(StrEnum):
    """Where a field appears on the seller's form."""

    BASIC = "basic"
    TECHNICAL = "technical"
    APPEARANCE = "appearance"
    CONDITION = "condition"
    SALE = "sale"


class UnknownFieldError(ValueError):
    pass


class NotObservableError(ValueError):
    """A photo cannot show this field (e.g. accident history), so a vision proposal is void."""


# Plain digits, or thousands grouped with "." "," or space ("120.000"). A value like
# "1.5" is rejected instead of silently becoming 15.
_INTEGER_PATTERN = re.compile(r"^\d+$|^\d{1,3}([.,\s]\d{3})+$")
_TRUE_WORDS = frozenset({"true", "evet", "var", "olur"})
_FALSE_WORDS = frozenset({"false", "hayır", "hayir", "yok", "olmaz"})


class FieldDefinition(StrictModel):
    key: FieldKey
    label_tr: str = Field(min_length=1)
    value_type: ValueType
    required: bool
    observable_from_photo: PhotoObservability
    group: FieldGroup = FieldGroup.BASIC
    choices: list[str] | None = None
    # Turkish display text per choice key; the stored value stays the key.
    choice_labels: dict[str, str] | None = None
    boolean_labels: tuple[str, str] = ("Var", "Yok")
    min_value: int | None = None
    max_value: int | None = None
    unit: str | None = None
    max_length: int = Field(default=200, ge=1)
    # Shown in the "Araç Bilgileri" list that code builds from approved facts.
    in_spec_table: bool = False
    # Absolute terms a seller's negative answer for this field supports ("görünür hasar: yok"
    # supports "hasar yok"). Used by the Safety Reviewer; still produces a warning.
    absence_terms: list[str] = Field(default_factory=list)
    placeholder: str | None = None
    multiline: bool = False
    note: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "FieldDefinition":
        if (self.value_type is ValueType.CHOICE) != bool(self.choices):
            raise ValueError(f"{self.key}: choices are required for, and only for, choice fields")
        if self.choice_labels is not None and set(self.choice_labels) != set(self.choices or []):
            raise ValueError(f"{self.key}: choice_labels must cover exactly the choices")
        has_bounds = self.min_value is not None or self.max_value is not None
        if has_bounds and self.value_type is not ValueType.INTEGER:
            raise ValueError(f"{self.key}: min/max only apply to integer fields")
        return self

    def choice_label(self, choice: str) -> str:
        return (self.choice_labels or {}).get(choice, tr_capitalize(choice))

    def normalize(self, raw: str) -> str:
        """Return the canonical string form of `raw`, or raise ValueError (Turkish message)."""
        label = self.label_tr
        value = raw.strip()
        if not value:
            raise ValueError(f"{label}: boş bırakılamaz.")

        match self.value_type:
            case ValueType.TEXT:
                if len(value) > self.max_length:
                    raise ValueError(f"{label}: en fazla {self.max_length} karakter olabilir.")
                return value
            case ValueType.INTEGER:
                if not _INTEGER_PATTERN.match(value):
                    raise ValueError(f"{label}: tam sayı olmalı (girilen: {value!r}).")
                number = int(re.sub(r"[.,\s]", "", value))
                if self.min_value is not None and number < self.min_value:
                    raise ValueError(f"{label}: en az {group_thousands(self.min_value)} olmalı.")
                if self.max_value is not None and number > self.max_value:
                    raise ValueError(
                        f"{label}: en fazla {group_thousands(self.max_value)} olabilir."
                    )
                return str(number)
            case ValueType.BOOLEAN:
                key = comparison_key(value)
                true_label, false_label = (comparison_key(v) for v in self.boolean_labels)
                if key in _TRUE_WORDS | {true_label}:
                    return "true"
                if key in _FALSE_WORDS | {false_label}:
                    return "false"
                raise ValueError(
                    f"{label}: '{self.boolean_labels[0]}' veya '{self.boolean_labels[1]}' olmalı."
                )
            case ValueType.CHOICE:
                key = comparison_key(value)
                for choice in self.choices or []:
                    if key in (choice, comparison_key(self.choice_label(choice))):
                        return choice
                options = ", ".join(self.choice_label(c) for c in self.choices or [])
                raise ValueError(f"{label}: şunlardan biri olmalı: {options}.")

    def display_value(self, value: str) -> str:
        """Seller-facing form of a stored value: 'manual' -> 'Manuel', '222000' -> '222.000 km'."""
        match self.value_type:
            case ValueType.CHOICE:
                return self.choice_label(value)
            case ValueType.BOOLEAN:
                return self.boolean_labels[0] if value == "true" else self.boolean_labels[1]
            case ValueType.INTEGER if value.isdigit():
                # Units mark quantities (km, TL); a model year must not become "2.013".
                if self.unit:
                    return f"{group_thousands(int(value))} {self.unit}"
                return value
            case _:
                return tr_capitalize(value)


class CategorySchema(StrictModel):
    category: Category
    version: int = Field(ge=1)
    field_definitions: list[FieldDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique_keys(self) -> "CategorySchema":
        keys = [definition.key for definition in self.field_definitions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate field keys in category schema")
        return self

    def get(self, key: str) -> FieldDefinition:
        for definition in self.field_definitions:
            if definition.key == key:
                return definition
        raise UnknownFieldError(f"Bilinmeyen alan: {key!r}.")

    @property
    def required_keys(self) -> list[str]:
        return [d.key for d in self.field_definitions if d.required]

    @property
    def photo_observable_keys(self) -> list[str]:
        return [
            d.key
            for d in self.field_definitions
            if d.observable_from_photo is not PhotoObservability.NO
        ]

    def normalize_value(self, key: str, raw: str) -> str:
        return self.get(key).normalize(raw)

    def display_value(self, key: str, value: str) -> str:
        return self.get(key).display_value(value)

    def validate_vision_proposal(self, proposal: VisionFactProposal) -> VisionFactProposal:
        """Reject proposals for fields a photo cannot show (e.g. accident history)."""
        definition = self.get(proposal.field_key)
        if definition.observable_from_photo is PhotoObservability.NO:
            raise NotObservableError(f"{proposal.field_key} cannot be observed from a photo")
        return proposal.model_copy(update={"value": definition.normalize(proposal.value)})


@cache
def load_category_schema(category: Category) -> CategorySchema:
    path = resources.files("listing_assistant") / "schemas" / f"{category.value}.json"
    return CategorySchema.model_validate_json(path.read_text(encoding="utf-8"))
