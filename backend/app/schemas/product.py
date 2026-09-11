"""Request/response schemas for Product and Selling Option (Phase 3 plan v3 §9/§13).

`ProductCreateRequest` deliberately has no `selling_options` field — Selling Options are
always created afterward via their own dedicated endpoints, never nested in a Product
write (Phase 3 plan v3 §9: no parent+nested-child write convention). `product_type` is
absent from `ProductUpdateRequest` — it is immutable after creation (Phase 3 plan v3 §10).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.enums import ProductType


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class ProductCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    product_type: ProductType
    description: str | None = Field(default=None, max_length=2000)
    default_packaging_cost: Decimal = Field(default=Decimal("0"), ge=0)
    can_reuse_surplus: bool = False
    # Only the existing DB CHECK (NULL or > 0) is enforced — no invented cross-field rule
    # against can_reuse_surplus (Phase 3 plan v3 §11): all four combinations are valid.
    default_surplus_usable_days: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name is required.")
        return stripped

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        return _strip_or_none(value)


class ProductUpdateRequest(BaseModel):
    """Partial update, Product-level fields only — never `product_type`, never
    `selling_options` (Phase 3 plan v3 §9/§10)."""

    version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    default_packaging_cost: Decimal | None = Field(default=None, ge=0)
    can_reuse_surplus: bool | None = None
    default_surplus_usable_days: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    # Remediation (Checkpoint 3 review): `name`/`default_packaging_cost`/
    # `can_reuse_surplus` are typed `| None` only so they can be *omitted* — the DB
    # columns themselves are NOT NULL. `description` and `default_surplus_usable_days`
    # ARE genuinely nullable, so explicit null stays a legitimate "clear this field"
    # request for those two and is deliberately excluded here. See the identical
    # rationale on `CustomerUpdateRequest`.
    @model_validator(mode="after")
    def _reject_explicit_null_for_non_nullable_fields(self) -> ProductUpdateRequest:
        non_nullable = ("name", "default_packaging_cost", "can_reuse_surplus")
        for field_name in non_nullable:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be set to null.")
        return self


class SellingOptionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity_units: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)
    packaging_cost: Decimal = Field(default=Decimal("0"), ge=0)
    sort_order: int = 0

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name is required.")
        return stripped


class SellingOptionUpdateRequest(BaseModel):
    version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    quantity_units: Decimal | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, ge=0)
    packaging_cost: Decimal | None = Field(default=None, ge=0)
    sort_order: int | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    # Remediation (Checkpoint 3 review): every mutable SellingOption field is NOT NULL —
    # there is no genuinely-nullable field on this schema (unlike Customer/Product), so
    # explicit null is rejected for all five.
    @model_validator(mode="after")
    def _reject_explicit_null_for_non_nullable_fields(self) -> SellingOptionUpdateRequest:
        non_nullable = ("name", "quantity_units", "price", "packaging_cost", "sort_order")
        for field_name in non_nullable:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be set to null.")
        return self


class LifecycleActionRequest(BaseModel):
    version: int


class SellingOptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    name: str
    quantity_units: Decimal
    price: Decimal
    packaging_cost: Decimal
    sort_order: int
    is_active: bool
    version: int


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    product_type: ProductType
    default_packaging_cost: Decimal
    can_reuse_surplus: bool
    default_surplus_usable_days: int | None
    is_active: bool
    version: int
    selling_options: list[SellingOptionResponse]


class ProductSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    product_type: ProductType
    is_active: bool
    version: int
