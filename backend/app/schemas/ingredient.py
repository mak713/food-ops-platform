"""Request/response schemas for the Ingredient resource (Phase 4 Plan v4 §4).

`measurement_family`/`canonical_unit` are immutable after creation (mirrors
`Product.product_type`'s immutability precedent) — validated for mutual consistency only
at creation, via the same closed unit set `UnitConversion` already enforces (Spec §7.5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.enums import MeasurementFamily
from app.domain.unit_conversion import UnknownUnitError, unit_family


class IngredientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    measurement_family: MeasurementFamily
    canonical_unit: str = Field(min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name is required.")
        return stripped

    @model_validator(mode="after")
    def _canonical_unit_must_match_measurement_family(self) -> IngredientCreateRequest:
        try:
            actual_family = unit_family(self.canonical_unit)
        except UnknownUnitError as exc:
            raise ValueError(str(exc)) from exc
        if actual_family is not self.measurement_family:
            raise ValueError(
                f"canonical_unit {self.canonical_unit!r} is not a "
                f"{self.measurement_family.value} unit."
            )
        return self


class IngredientUpdateRequest(BaseModel):
    """Partial update — name only. `measurement_family`/`canonical_unit` are immutable
    after creation and deliberately excluded here; `is_active` goes through the dedicated
    archive/reactivate actions only (Spec §11.8 — no generic client-controlled status
    PATCH)."""

    version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    @model_validator(mode="after")
    def _reject_explicit_null_for_non_nullable_fields(self) -> IngredientUpdateRequest:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be set to null.")
        return self


class LifecycleActionRequest(BaseModel):
    """Body for archive/reactivate actions — carries only the version the client last
    read (matching the Customer/Product/SellingOption precedent)."""

    version: int


class IngredientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    measurement_family: MeasurementFamily
    canonical_unit: str
    is_active: bool
    version: int


class IngredientSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    measurement_family: MeasurementFamily
    canonical_unit: str
    is_active: bool
    version: int
