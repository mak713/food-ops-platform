"""Deterministic Order-domain pure calculators (Phase 6 Final Plan §D/§F/§H; Final
Pre-Implementation Amendment §11).

Pure, side-effect-free: no HTTP or database access, matching `inventory_costing.py`'s/
`recipe_scaling.py`'s precedent exactly (ADR-109). All Decimal arithmetic runs inside an
explicit, fixed-precision `decimal.localcontext()` so results never depend on — or leak
into — whatever ambient `decimal.getcontext()` the caller has set.

Quantization constants are declared locally here rather than imported from
`app.domain.inventory_costing` — Order domain code stays independent of the Phase 5
inventory module despite the coincidentally-matching `NUMERIC(18,6)` quantity shape (Final
Pre-Implementation Amendment §5/§11). The money rule (`NUMERIC(14,2)`, `ROUND_HALF_UP`) is
an explicit Phase 6 decision, not an inherited Phase 5 rule.
"""

from __future__ import annotations

import decimal
import enum
from dataclasses import dataclass
from decimal import Decimal

_CONTEXT_PRECISION = 50

#: Money = NUMERIC(14,2). Approved Phase 6 decision (Final Plan §F; Amendment §11).
_MONEY_QUANTUM = Decimal("0.01")
_MONEY_ROUNDING = decimal.ROUND_HALF_UP
#: 14 total digits, 2 fractional -> 12 integer digits, independent of sign.
MONEY_MAX_MAGNITUDE = Decimal("999999999999.99")

#: Quantity = NUMERIC(18,6). Approved Phase 6 decision, declared locally per Amendment §11.
_QUANTITY_QUANTUM = Decimal("0.000001")
_QUANTITY_ROUNDING = decimal.ROUND_HALF_UP
#: 18 total digits, 6 fractional -> 12 integer digits, independent of sign.
QUANTITY_MAX_MAGNITUDE = Decimal("999999999999.999999")


def quantize_money(value: Decimal) -> Decimal:
    """The sole quantization point for a computed value about to be persisted into a
    `NUMERIC(14,2)` Order/OrderLine/Payment column. Domain calculators below always
    return unrounded, full-precision results; only this function (and its quantity
    counterpart) ever rounds a value that is about to be stored."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_MONEY_QUANTUM, rounding=_MONEY_ROUNDING)


def is_representable_in_numeric_14_2(value: Decimal) -> bool:
    """True if `value` fits within the frozen `NUMERIC(14,2)` column shape. Callers should
    pass an already-`quantize_money`d value — this checks magnitude only, not scale."""
    return abs(value) <= MONEY_MAX_MAGNITUDE


def quantize_quantity(value: Decimal) -> Decimal:
    """The sole quantization point for a computed value about to be persisted into a
    `NUMERIC(18,6)` OrderLine quantity column."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_QUANTITY_QUANTUM, rounding=_QUANTITY_ROUNDING)


def is_representable_in_numeric_18_6(value: Decimal) -> bool:
    """True if `value` fits within the frozen `NUMERIC(18,6)` column shape. Callers should
    pass an already-`quantize_quantity`d value — this checks magnitude only, not scale."""
    return abs(value) <= QUANTITY_MAX_MAGNITUDE


@dataclass(frozen=True)
class LineSnapshot:
    """The full set of snapshot fields captured for one OrderLine — returned unrounded;
    the service layer quantizes every field exactly once before persistence."""

    display_name_snapshot: str
    package_quantity: Decimal
    underlying_quantity: Decimal
    charged_unit_price_snapshot: Decimal
    line_subtotal: Decimal
    packaging_cost_per_package_snapshot: Decimal
    packaging_cost_total_snapshot: Decimal


def calculate_line_subtotal(package_quantity: Decimal, charged_unit_price: Decimal) -> Decimal:
    """`package_quantity * charged_unit_price` — the single arithmetic rule shared by every
    line type's `line_subtotal` (Final Plan §F's arithmetic-order rule: derive from
    already-stored representations, never a fresh catalog read)."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return package_quantity * charged_unit_price


def calculate_packaging_total(
    package_quantity: Decimal, packaging_cost_per_package: Decimal
) -> Decimal:
    """`package_quantity * packaging_cost_per_package` — shared by every line type's
    `packaging_cost_total_snapshot`."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return package_quantity * packaging_cost_per_package


def calculate_standard_option_line(
    *,
    display_name: str,
    package_quantity: Decimal,
    underlying_units_per_package: Decimal,
    charged_unit_price: Decimal,
    packaging_cost_per_package: Decimal,
) -> LineSnapshot:
    """Full snapshot capture for a STANDARD_OPTION line — used at line creation and at an
    actual source-identity change (Final Plan §D.2 check 1 / "Actual source identity
    change" §STANDARD_OPTION). `charged_unit_price` is whatever the caller already
    resolved (the Selling Option's live catalog price, or an explicit seller override —
    the caller decides which; this function only multiplies). A quantity-only edit does
    NOT call this — see `rescale_standard_option_line` below, which reuses the line's own
    already-stored per-unit basis instead of a live catalog re-fetch.

    Example (Spec §8.16): 2 x 6-pack at $12/package -> package_quantity=2,
    underlying_quantity=12, charged_unit_price_snapshot=12.00, line_subtotal=24.00."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        underlying_quantity = package_quantity * underlying_units_per_package
    line_subtotal = calculate_line_subtotal(package_quantity, charged_unit_price)
    packaging_cost_total = calculate_packaging_total(package_quantity, packaging_cost_per_package)
    return LineSnapshot(
        display_name_snapshot=display_name,
        package_quantity=package_quantity,
        underlying_quantity=underlying_quantity,
        charged_unit_price_snapshot=charged_unit_price,
        line_subtotal=line_subtotal,
        packaging_cost_per_package_snapshot=packaging_cost_per_package,
        packaging_cost_total_snapshot=packaging_cost_total,
    )


def rescale_standard_option_line(
    *,
    display_name: str,
    new_package_quantity: Decimal,
    stored_package_quantity: Decimal,
    stored_underlying_quantity: Decimal,
    stored_charged_unit_price_snapshot: Decimal,
    stored_packaging_cost_per_package_snapshot: Decimal,
) -> LineSnapshot:
    """Quantity-only edit (Final Plan §D.2 check 4 / §11 "Quantity-only edit") for
    STANDARD_OPTION — reapplies the line's own already-stored underlying-quantity /
    package-quantity ratio to the new `package_quantity`, and reapplies the already-stored
    per-unit price/packaging rates. Never consults live Selling Option data — that is
    exactly why this exists as a function distinct from `calculate_standard_option_line`:
    a catalog price/packaging/multiplier change made after this line was captured must
    never leak into a pure quantity edit."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        ratio = stored_underlying_quantity / stored_package_quantity
        underlying_quantity = new_package_quantity * ratio
    line_subtotal = calculate_line_subtotal(
        new_package_quantity, stored_charged_unit_price_snapshot
    )
    packaging_cost_total = calculate_packaging_total(
        new_package_quantity, stored_packaging_cost_per_package_snapshot
    )
    return LineSnapshot(
        display_name_snapshot=display_name,
        package_quantity=new_package_quantity,
        underlying_quantity=underlying_quantity,
        charged_unit_price_snapshot=stored_charged_unit_price_snapshot,
        line_subtotal=line_subtotal,
        packaging_cost_per_package_snapshot=stored_packaging_cost_per_package_snapshot,
        packaging_cost_total_snapshot=packaging_cost_total,
    )


def calculate_custom_quantity_line(
    *,
    display_name: str,
    underlying_quantity: Decimal,
    agreed_line_price: Decimal,
    packaging_cost_per_package: Decimal,
) -> LineSnapshot:
    """CUSTOM_QUANTITY: `package_quantity` is always exactly `1` (Final Plan §E; Amendment
    §3/§12) — `charged_unit_price_snapshot` IS the whole line's seller-agreed price, not a
    per-unit rate, because `package_quantity=1` collapses the package/price multiplication
    to a no-op. No Product catalog price exists for this line type — `agreed_line_price`
    is always seller-entered, for every edit dimension, source change included (Amendment
    §4: "there is no Product catalog selling price").

    Example (Spec §8.16): 30 cookies for an agreed $55 -> package_quantity=1,
    underlying_quantity=30, charged_unit_price_snapshot=55.00, line_subtotal=55.00."""
    package_quantity = Decimal(1)
    line_subtotal = calculate_line_subtotal(package_quantity, agreed_line_price)
    packaging_cost_total = calculate_packaging_total(package_quantity, packaging_cost_per_package)
    return LineSnapshot(
        display_name_snapshot=display_name,
        package_quantity=package_quantity,
        underlying_quantity=underlying_quantity,
        charged_unit_price_snapshot=agreed_line_price,
        line_subtotal=line_subtotal,
        packaging_cost_per_package_snapshot=packaging_cost_per_package,
        packaging_cost_total_snapshot=packaging_cost_total,
    )


def calculate_custom_item_line(
    *,
    display_name: str,
    quantity: Decimal,
    unit_price: Decimal,
) -> LineSnapshot:
    """CUSTOM_ITEM: no packaging input exists (Final Plan §B/§E — "no authoritative basis
    for Custom Item packaging") — packaging snapshots are always `0`. `package_quantity ==
    underlying_quantity == quantity`: there is no package/underlying distinction without a
    Selling Option (Final Plan §E)."""
    line_subtotal = calculate_line_subtotal(quantity, unit_price)
    zero = Decimal("0")
    return LineSnapshot(
        display_name_snapshot=display_name,
        package_quantity=quantity,
        underlying_quantity=quantity,
        charged_unit_price_snapshot=unit_price,
        line_subtotal=line_subtotal,
        packaging_cost_per_package_snapshot=zero,
        packaging_cost_total_snapshot=zero,
    )


@dataclass(frozen=True)
class OrderTotals:
    subtotal: Decimal
    final_total: Decimal


def calculate_order_totals(
    line_subtotals: list[Decimal], *, order_adjustment: Decimal, manual_tax: Decimal
) -> OrderTotals:
    """`subtotal = SUM(stored line_subtotal)`; `final_total = subtotal + order_adjustment +
    manual_tax` (ORD-012/ORD-013; Final Plan §F). Callers pass already-quantized,
    already-persisted `line_subtotal`/`order_adjustment`/`manual_tax` values — every
    derived total therefore traces to stored representations, never freshly-recomputed
    live inputs."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        subtotal = sum(line_subtotals, start=Decimal(0))
        final_total = subtotal + order_adjustment + manual_tax
    return OrderTotals(subtotal=subtotal, final_total=final_total)


class PaymentStatus(enum.StrEnum):
    """Response-only — never persisted (Spec §8.17: "Payment status is derived; it is not
    a stored status field")."""

    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"


def derive_payment_status(payments_total: Decimal, final_total: Decimal) -> PaymentStatus:
    """AC-PAY-002 thresholds, checked in this exact order (Final Pre-Implementation
    Amendment §10): the zero-payment check runs FIRST and unconditionally, so a $0.00
    Order with $0.00 recorded Payments is UNPAID, never PAID merely because `0 >= 0`. Only
    once `payments_total > 0` does the `>= final_total` comparison ever apply — which also
    correctly covers overpayment (PAY-003)."""
    if payments_total == 0:
        return PaymentStatus.UNPAID
    if payments_total < final_total:
        return PaymentStatus.PARTIALLY_PAID
    return PaymentStatus.PAID


@dataclass(frozen=True)
class ConfirmationIssue:
    """Typed, machine-readable structural-confirmation-readiness issue (Final
    Pre-Implementation Amendment §5) — callers should branch on `code`, not parse
    `message`."""

    severity: str
    code: str
    message: str
    field: str | None = None


def check_structural_confirmation_readiness(
    *, status: str, line_count: int, fulfillment_date_present: bool
) -> list[ConfirmationIssue]:
    """ORD-004 only (Final Plan §C/§H): current status is DRAFT; at least one OrderLine;
    fulfillment_date present. Pure — no DB access, no re-validation of any reference's
    active-state at all, so a carried-forward archived reference can never cause a false
    rejection here (ADR-108). Never mutates anything, never checks anything beyond ORD-004
    — Phase 6 does not persist CONFIRMED under any circumstance; this function only reports
    whether the *structural* prerequisites are met."""
    issues: list[ConfirmationIssue] = []
    if status != "DRAFT":
        issues.append(
            ConfirmationIssue(
                severity="ERROR",
                code="ORDER_STATUS_NOT_DRAFT",
                message="Only a Draft order can be confirmed.",
                field="status",
            )
        )
    if line_count < 1:
        issues.append(
            ConfirmationIssue(
                severity="ERROR",
                code="ORDER_NO_LINES",
                message="At least one order line is required before confirmation.",
                field="lines",
            )
        )
    if not fulfillment_date_present:
        issues.append(
            ConfirmationIssue(
                severity="ERROR",
                code="ORDER_MISSING_FULFILLMENT_DATE",
                message="A fulfillment date is required before confirmation.",
                field="fulfillment_date",
            )
        )
    return issues
