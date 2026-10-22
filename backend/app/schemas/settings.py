"""Request/response schemas for Business Settings and the owner profile (Phase 11; Spec
AUTH-007, SET-001..005, §8.4).

Validation uses only constraints the Spec/schema actually ground: NOT NULL columns cannot be
cleared; the money/ratio fields mirror their `NUMERIC` column bounds (`default_labor_rate`
>= 0 with 2 decimal places, `target_contribution_margin` in [0, 1) with 6 decimal places —
never silently rounded); `shopping_horizon_days` is exactly 3 or 7; and
`fulfillment_warning_minutes` is `> 0` with the real PostgreSQL `INTEGER` limit as its only
upper bound (a persistence limit, not a product rule — the Dashboard comparison is
overflow-safe for every storable value). Free-text profile fields have no schema length
limits, so none are invented here beyond the project's existing name/email conventions.

`default_fulfillment_method` is intentionally absent from every schema in this module
(Phase 11 scope decision): unknown request keys — including `business_id`,
`owner_user_id`, `id` and `default_fulfillment_method` — are ignored by Pydantic, never
applied.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, StrictInt, field_validator

from app.core.timezone import validate_iana_timezone
from app.schemas.auth import normalize_email

POSTGRES_INTEGER_MAX = 2_147_483_647
"""PostgreSQL `INTEGER` upper bound — the only ceiling on `fulfillment_warning_minutes`."""

_MONEY_QUANTUM = Decimal("0.01")
_RATIO_QUANTUM = Decimal("0.000001")


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class BusinessSettingsResponse(BaseModel):
    id: str
    name: str
    business_type: str | None
    contact_phone: str | None
    contact_email: str | None
    address_text: str | None
    timezone: str
    default_labor_rate: Decimal
    target_contribution_margin: Decimal | None
    fulfillment_warning_minutes: int
    shopping_horizon_days: int
    version: int

    @field_validator("default_labor_rate")
    @classmethod
    def _money_two_places(cls, value: Decimal) -> Decimal:
        return value.quantize(_MONEY_QUANTUM)

    @field_validator("target_contribution_margin")
    @classmethod
    def _ratio_six_places(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else value.quantize(_RATIO_QUANTUM)


class BusinessSettingsUpdateRequest(BaseModel):
    """Partial update: an absent field is unchanged; an explicit `null` clears a nullable
    field. NOT NULL fields reject an explicit `null` with a normal `422`."""

    version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    business_type: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = Field(default=None, max_length=254)
    address_text: str | None = None
    timezone: str | None = Field(default=None, max_length=100)
    default_labor_rate: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    target_contribution_margin: Decimal | None = Field(
        default=None, ge=0, lt=1, max_digits=9, decimal_places=6
    )
    fulfillment_warning_minutes: StrictInt | None = Field(
        default=None, ge=1, le=POSTGRES_INTEGER_MAX
    )
    shopping_horizon_days: StrictInt | None = None

    # Validators only run for fields the client actually sent, so `None` reaching one of the
    # NOT NULL fields below means an explicit `null` — rejected here with a field-level 422
    # rather than reaching the database as a raw IntegrityError.
    @field_validator(
        "name",
        "timezone",
        "default_labor_rate",
        "fulfillment_warning_minutes",
        "shopping_horizon_days",
        mode="before",
    )
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("This setting cannot be cleared.")
        return value

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Business name cannot be blank.")
        return stripped

    @field_validator("business_type", "contact_phone", "address_text")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    @field_validator("contact_email")
    @classmethod
    def _validate_contact_email(cls, value: str | None) -> str | None:
        stripped = _strip_or_none(value)
        if stripped is None:
            return None
        normalize_email(stripped)  # shape check only; the owner's casing is preserved
        return stripped

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        return validate_iana_timezone(value)

    @field_validator("shopping_horizon_days")
    @classmethod
    def _validate_horizon(cls, value: int) -> int:
        if value not in (3, 7):
            raise ValueError("Shopping List horizon must be 3 or 7 days.")
        return value


class OwnerProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped
