"""Request/response schemas for Order, OrderLine, and Payment (Phase 6 Final Plan §D/§H).

`OrderLineInput` covers create AND update: `id` present (and matching an existing line
on this Order) means "retained, edit in place"; `id` absent/`None` means "new line";
an existing line whose `id` is not resubmitted is deleted — the full stable-ID
reconciliation algorithm lives in `app.services.order_service` (Final Plan §D.2). The
line-type shape validator below mirrors the frozen `ck_order_lines_line_type_shape`
DB CHECK exactly, rejecting an impossible field combination before any DB access;
reference *existence*/*active-state* (which needs locked DB rows) is validated in the
service layer, never here.

Raw seller-entered money/quantity fields are constrained via Pydantic's `max_digits`/
`decimal_places`, matching the live convention in `app/schemas/inventory.py`/
`app/schemas/recipe.py` exactly (Final Plan §F) — excess precision is a clean 422, never
silently rounded.
"""

from __future__ import annotations

import uuid
from datetime import date, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import FulfillmentMethod, OrderLineType, OrderStatus


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class OrderLineInput(BaseModel):
    id: uuid.UUID | None = None
    line_type: OrderLineType

    # STANDARD_OPTION / CUSTOM_QUANTITY
    product_id: uuid.UUID | None = None
    selling_option_id: uuid.UUID | None = None  # STANDARD_OPTION only

    # STANDARD_OPTION: seller-entered package count, freely editable.
    package_quantity: Decimal | None = Field(default=None, max_digits=18, decimal_places=6, gt=0)
    # CUSTOM_QUANTITY / CUSTOM_ITEM: seller-entered real unit count.
    underlying_quantity: Decimal | None = Field(default=None, max_digits=18, decimal_places=6, gt=0)

    # STANDARD_OPTION: explicit price override (optional — falls back to the Selling
    # Option's catalog price when omitted). CUSTOM_QUANTITY: the seller-agreed whole-line
    # price (required). CUSTOM_ITEM: the seller-entered unit price (required).
    charged_unit_price: Decimal | None = Field(default=None, max_digits=14, decimal_places=2, ge=0)
    price_override_reason: str | None = Field(default=None, max_length=500)

    # CUSTOM_QUANTITY only — no packaging input exists for CUSTOM_ITEM (always 0) or
    # STANDARD_OPTION (catalog-derived, only refreshed via an actual source change).
    packaging_cost_per_package: Decimal | None = Field(
        default=None, max_digits=14, decimal_places=2, ge=0
    )

    display_name: str | None = Field(default=None, max_length=200)  # CUSTOM_ITEM only
    custom_direct_cost_estimate: Decimal | None = Field(
        default=None, max_digits=14, decimal_places=2, ge=0
    )
    # Bounded to PostgreSQL's signed 32-bit INTEGER range (Checkpoint-3 correction 11) —
    # Pydantic's plain `ge=0` alone does not bound Python's unbounded int, so an
    # enormous value would otherwise pass request validation and only fail as a raw
    # database integer-overflow error.
    custom_active_time_minutes: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # CUSTOM_ITEM only; omitted -> service applies the Phase 6 default (True on creation,
    # the existing stored value on an otherwise-unrelated edit to a retained line).
    manual_fulfillment_required: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _strip_text_fields(self) -> OrderLineInput:
        self.price_override_reason = _strip_or_none(self.price_override_reason)
        self.display_name = _strip_or_none(self.display_name)
        self.notes = _strip_or_none(self.notes)
        return self

    @model_validator(mode="after")
    def _validate_shape(self) -> OrderLineInput:
        """Mirrors `ck_order_lines_line_type_shape` plus the line-type-specific required
        seller inputs (Final Plan §E) — a pure shape check, no DB access. Also rejects
        fields the architecture defines as not applicable to a given line type
        (Checkpoint-3 correction 12) — an invalid field combination is a clean 422 here,
        never silently discarded downstream."""
        if self.line_type is OrderLineType.STANDARD_OPTION:
            if self.product_id is None or self.selling_option_id is None:
                raise ValueError(
                    "STANDARD_OPTION lines require both product_id and selling_option_id."
                )
            if self.package_quantity is None:
                raise ValueError("STANDARD_OPTION lines require package_quantity.")
            if self.custom_direct_cost_estimate is not None:
                raise ValueError("STANDARD_OPTION lines must not set custom_direct_cost_estimate.")
            if self.custom_active_time_minutes is not None:
                raise ValueError("STANDARD_OPTION lines must not set custom_active_time_minutes.")
            if self.packaging_cost_per_package is not None:
                raise ValueError("STANDARD_OPTION lines must not set packaging_cost_per_package.")
        elif self.line_type is OrderLineType.CUSTOM_QUANTITY:
            if self.product_id is None:
                raise ValueError("CUSTOM_QUANTITY lines require product_id.")
            if self.selling_option_id is not None:
                raise ValueError("CUSTOM_QUANTITY lines must not reference a selling_option_id.")
            if self.underlying_quantity is None:
                raise ValueError("CUSTOM_QUANTITY lines require underlying_quantity.")
            if self.charged_unit_price is None:
                raise ValueError("CUSTOM_QUANTITY lines require charged_unit_price.")
            if self.package_quantity is not None:
                raise ValueError(
                    "CUSTOM_QUANTITY lines must not set package_quantity "
                    "(the server always fixes it to 1)."
                )
            if self.custom_direct_cost_estimate is not None:
                raise ValueError("CUSTOM_QUANTITY lines must not set custom_direct_cost_estimate.")
            if self.custom_active_time_minutes is not None:
                raise ValueError("CUSTOM_QUANTITY lines must not set custom_active_time_minutes.")
        else:  # CUSTOM_ITEM
            if self.product_id is not None or self.selling_option_id is not None:
                raise ValueError(
                    "CUSTOM_ITEM lines must not reference a Product or Selling Option."
                )
            if self.underlying_quantity is None:
                raise ValueError("CUSTOM_ITEM lines require underlying_quantity.")
            if self.charged_unit_price is None:
                raise ValueError("CUSTOM_ITEM lines require charged_unit_price.")
            if not self.display_name:
                raise ValueError("CUSTOM_ITEM lines require a display_name.")
            if self.package_quantity is not None:
                raise ValueError("CUSTOM_ITEM lines must not set package_quantity.")
            if self.packaging_cost_per_package is not None:
                raise ValueError("CUSTOM_ITEM lines must not set packaging_cost_per_package.")
            if self.price_override_reason:
                raise ValueError("CUSTOM_ITEM lines must not set price_override_reason.")
        return self


class PaymentCreateRequest(BaseModel):
    amount: Decimal = Field(max_digits=14, decimal_places=2, gt=0)
    payment_method: str = Field(min_length=1, max_length=100)
    payment_date: date
    notes: str | None = Field(default=None, max_length=1000)
    # Overpayment warning/acknowledgment (Final Plan §G) — False by default; the client
    # resubmits with True after acknowledging a PAYMENT_OVERAGE_WARNING.
    confirm_overpayment: bool = False

    @model_validator(mode="after")
    def _strip_text(self) -> PaymentCreateRequest:
        self.payment_method = self.payment_method.strip()
        if not self.payment_method:
            raise ValueError("payment_method is required.")
        self.notes = _strip_or_none(self.notes)
        return self


class PaymentInitialInput(PaymentCreateRequest):
    """Identical shape to `PaymentCreateRequest` — the optional initial Payment carried
    inside an atomic `OrderCreateRequest` (Final Plan §I: one atomic transaction for
    Draft+Lines+optional initial Payment)."""


class _OrderHeaderMixin(BaseModel):
    customer_id: uuid.UUID | None = None
    fulfillment_date: date | None = None
    fulfillment_time: time | None = None
    fulfillment_method: FulfillmentMethod | None = None
    fulfillment_details: str | None = Field(default=None, max_length=2000)
    fulfillment_notes: str | None = Field(default=None, max_length=2000)
    internal_notes: str | None = Field(default=None, max_length=2000)
    order_adjustment: Decimal = Field(default=Decimal("0"), max_digits=14, decimal_places=2)
    adjustment_description: str | None = Field(default=None, max_length=500)
    manual_tax: Decimal = Field(default=Decimal("0"), max_digits=14, decimal_places=2, ge=0)
    lines: list[OrderLineInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _strip_text_fields(self) -> _OrderHeaderMixin:
        self.fulfillment_details = _strip_or_none(self.fulfillment_details)
        self.fulfillment_notes = _strip_or_none(self.fulfillment_notes)
        self.internal_notes = _strip_or_none(self.internal_notes)
        self.adjustment_description = _strip_or_none(self.adjustment_description)
        return self

    @model_validator(mode="after")
    def _adjustment_requires_description(self) -> _OrderHeaderMixin:
        """`ck_orders_adjustment_description_required` mirrored at the app layer for a
        clean 422 rather than a raw IntegrityError (ORD-012)."""
        if self.order_adjustment != 0 and not self.adjustment_description:
            raise ValueError("adjustment_description is required when order_adjustment is nonzero.")
        return self


class OrderCreateRequest(_OrderHeaderMixin):
    payment: PaymentInitialInput | None = None


class OrderUpdateRequest(_OrderHeaderMixin):
    version: int
    # Draft-total-reduction overpayment guard (Final Plan §G) — same flag/shape as
    # PaymentCreateRequest's, since both protect the identical invariant.
    confirm_overpayment: bool = False


class LifecycleActionRequest(BaseModel):
    version: int


# --- Responses -------------------------------------------------------------------------


class OrderLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    line_type: OrderLineType
    product_id: str | None
    selling_option_id: str | None
    display_name_snapshot: str
    package_quantity: Decimal
    underlying_quantity: Decimal
    charged_unit_price_snapshot: Decimal
    line_subtotal: Decimal
    packaging_cost_per_package_snapshot: Decimal
    packaging_cost_total_snapshot: Decimal
    price_override_reason: str | None
    custom_direct_cost_estimate: Decimal | None
    custom_active_time_minutes: int | None
    manual_fulfillment_required: bool
    manual_fulfillment_satisfied: bool
    notes: str | None


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    amount: Decimal
    payment_method: str
    payment_date: date
    notes: str | None


class ConfirmationIssueResponse(BaseModel):
    severity: str
    code: str
    message: str
    field: str | None = None


class OrderResponse(BaseModel):
    id: str
    order_number: str
    status: OrderStatus
    customer_id: str | None
    fulfillment_date: date | None
    fulfillment_time: time | None
    fulfillment_method: FulfillmentMethod | None
    fulfillment_details: str | None
    fulfillment_notes: str | None
    internal_notes: str | None
    subtotal: Decimal
    order_adjustment: Decimal
    adjustment_description: str | None
    manual_tax: Decimal
    final_total: Decimal
    # Always NULL throughout Phase 6 — no authoritative calculator exists yet (Final Plan
    # §B/§C). Never zero, never fabricated.
    estimated_direct_cost: Decimal | None
    estimated_contribution: Decimal | None
    estimated_contribution_margin: Decimal | None
    version: int
    lines: list[OrderLineResponse]
    payments: list[PaymentResponse]
    payments_total: Decimal
    payment_status: str
    overpayment_amount: Decimal | None
    is_confirmable: bool
    confirmation_issues: list[ConfirmationIssueResponse]


class OrderSummary(BaseModel):
    id: str
    order_number: str
    status: OrderStatus
    customer_id: str | None
    customer_name: str | None
    fulfillment_date: date | None
    final_total: Decimal
    payment_status: str
    version: int
