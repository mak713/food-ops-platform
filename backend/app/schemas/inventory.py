"""Request/response schemas for Ingredient and Purchased Product Inventory (Phase 5 Plan
§C). Every quantity/cost field is constrained to the frozen `NUMERIC(18,6)` column shape
via Pydantic's `max_digits`/`decimal_places`, matching `app/schemas/recipe.py`'s existing
pattern exactly. Raw client-submitted values with more than 6 decimal places are rejected
(422) here, at the request boundary — never rounded; rounding only ever applies to a
value a domain calculator *derives*, at the service layer (`inventory_costing
.quantize_for_storage`), never to a value the client typed in directly.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManualAdjustmentReason(enum.StrEnum):
    """Preset manual-adjustment reasons (Spec §15.7) — reused identically for both
    Ingredient and Purchased Product Inventory adjustments (Phase 5 Plan §C); the spec
    only enumerates this list once, under Ingredient inventory mutation, and nothing
    suggests a distinct list for Purchased goods."""

    COUNT_CORRECTION = "COUNT_CORRECTION"
    SPOILAGE_OR_WASTE = "SPOILAGE_OR_WASTE"
    PERSONAL_OR_INTERNAL_USE = "PERSONAL_OR_INTERNAL_USE"
    DAMAGE = "DAMAGE"
    OTHER = "OTHER"


# --- Ingredient inventory requests ----------------------------------------------------


class IngredientInitialBalanceRequest(BaseModel):
    """Initial Balance mutates the already-existing, versioned Ingredient aggregate
    (Phase 5 Plan approval decision 3), so it carries `version` like any other Ingredient
    mutation — unlike Purchased Product Inventory's Initial Balance, where no row exists
    yet to have a version at all."""

    version: int
    quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    unit: str = Field(min_length=1, max_length=10)
    unit_cost: Decimal = Field(max_digits=18, decimal_places=6, ge=0)
    supplier_text: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class IngredientRestockRequest(BaseModel):
    version: int
    quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    unit: str = Field(min_length=1, max_length=10)
    unit_cost: Decimal = Field(max_digits=18, decimal_places=6, ge=0)
    supplier_text: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class IngredientAdjustmentRequest(BaseModel):
    version: int
    quantity_change: Decimal = Field(max_digits=18, decimal_places=6)
    reason: ManualAdjustmentReason
    notes: str | None = None

    @model_validator(mode="after")
    def _quantity_change_must_be_nonzero(self) -> IngredientAdjustmentRequest:
        if self.quantity_change == 0:
            raise ValueError("quantity_change must not be zero.")
        return self


class ReplacementCostRequest(BaseModel):
    """Shared by both Ingredient and Purchased Product Inventory (Phase 5 Plan approval
    decision 5) — sets an explicit seller-maintained override when `replacement_unit_cost`
    is provided, or clears it back to automatic fallback-to-Latest when explicitly passed
    as `null`. `replacement_unit_cost` is required (no default) but nullable — omitting it
    entirely is a `422`, never silently treated as `null`. This distinction matters
    because `null` is a meaningful instruction here ("clear the override"), not merely
    "not provided" — collapsing the two (as an earlier revision of this schema did via
    `default=None`) would let a client that forgets the field accidentally clear an
    override it never intended to touch (Phase 5 correction-pass finding 2)."""

    version: int
    replacement_unit_cost: Decimal | None = Field(max_digits=18, decimal_places=6, ge=0)


# --- Purchased Product Inventory requests ---------------------------------------------


class PurchasedInitialBalanceRequest(BaseModel):
    """No `version` — Purchased Product Inventory's Initial Balance always creates the
    row (Phase 5 Plan approval decision 3); there is nothing to have a version yet. No
    `unit` either — Purchased Product Inventory has no `measurement_family`/
    `canonical_unit`; `quantity` is a plain Decimal count of the Product's own units, not
    necessarily a whole number."""

    quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    unit_cost: Decimal = Field(max_digits=18, decimal_places=6, ge=0)
    supplier_text: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class PurchasedRestockRequest(BaseModel):
    """`version` is nullable (Phase 5 Plan approval decision 4): `null`/omitted asserts
    "I believe no inventory row exists yet for this Product" (a possible first-ever
    purchase event); a concrete `version` asserts "I'm restocking an inventory row I
    already know about." Whichever the caller asserts is verified server-side — an
    absent version is never treated as license to overwrite a row that already exists."""

    version: int | None = None
    quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    unit_cost: Decimal = Field(max_digits=18, decimal_places=6, ge=0)
    supplier_text: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class PurchasedAdjustmentRequest(BaseModel):
    version: int
    quantity_change: Decimal = Field(max_digits=18, decimal_places=6)
    reason: ManualAdjustmentReason
    notes: str | None = None

    @model_validator(mode="after")
    def _quantity_change_must_be_nonzero(self) -> PurchasedAdjustmentRequest:
        if self.quantity_change == 0:
            raise ValueError("quantity_change must not be zero.")
        return self


# --- Responses -------------------------------------------------------------------------


class InventoryTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    transaction_type: str
    quantity_change: Decimal
    unit_cost: Decimal | None
    total_cost: Decimal | None
    supplier_text: str | None
    reason: str | None
    notes: str | None
    created_at: datetime


class PurchasedProductInventoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: str
    physical_quantity: Decimal
    weighted_average_unit_cost: Decimal
    latest_purchase_unit_cost: Decimal | None
    replacement_unit_cost: Decimal | None
    effective_replacement_cost: Decimal | None
    version: int


class PurchasedProductInventorySummary(BaseModel):
    """Powers the top-level Inventory-module list (`GET /api/v1/purchased-inventory`) —
    every field below `product_id`/`product_name` is `None` when the Product has never
    had its inventory initialized yet (Phase 5 Plan §F)."""

    model_config = ConfigDict(from_attributes=True)

    product_id: str
    product_name: str
    product_is_active: bool
    physical_quantity: Decimal | None
    weighted_average_unit_cost: Decimal | None
    latest_purchase_unit_cost: Decimal | None
    replacement_unit_cost: Decimal | None
    effective_replacement_cost: Decimal | None
    version: int | None
