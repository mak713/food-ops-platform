"""Deterministic Order Completion planning (Spec §7.15/§7.16/§15.13/§15.14; ADR-051/052/057/
058/059; Order Completion / Fulfillment slice).

Pure, side-effect-free: no HTTP or database access and no `ApiError` import (ADR-109 and its
Phase 7 addendum — a sufficiency or representability failure is returned as a plain value;
translating it into a structured `409`/rollback is the service layer's job). Every Decimal
operation runs in its own explicit, fixed-precision `decimal.localcontext()`.

The service gathers locked, persisted facts and hands them here; this module builds the exact
mutation plan and the exact historical ledger rows, quantizing each derived value **once**
(ADR-115/119):

* `PURCHASED_GOOD` — quantity x the inventory's Weighted Average Unit Cost snapshot. The
  transaction's `total_cost` is `quantity x unit_cost` quantized once to six decimals (so a
  stored `total_cost` is always exactly the product of that row's own stored quantity and
  unit cost, ADR-115); the ledger `amount` is that persisted `total_cost` quantized once to
  cents. Latest, replacement and current Product cost never participate (ADR-059).
* `SURPLUS` — each consumed allocation's quantity x its lot's persisted `unit_cost_basis`,
  quantized once to cents (Spec §7.16 step 4, ADR-058). No residual reconciliation against
  `source_total_cost_basis` (which remains immutable Production-Finish provenance).
* `PACKAGING` — the line's immutable `packaging_cost_total_snapshot` copied exactly, for
  every line, including an explicit `0.00`.
* `CUSTOM_DIRECT` — the line's `custom_direct_cost_estimate` copied exactly whenever it is
  not `NULL` (explicit `0.00` included). `NULL` produces no row and is never guessed.

Known-zero is distinct from absent: zero-valued Purchased/Surplus/Packaging amounts are
planned as explicit `0.00` rows.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.order_pricing import (
    is_representable_in_numeric_14_2,
    is_representable_in_numeric_18_6,
)

_CONTEXT_PRECISION = 50
_QUANTITY_QUANTUM = Decimal("0.000001")
_MONEY_QUANTUM = Decimal("0.01")
_ROUNDING = decimal.ROUND_HALF_UP
"""Round-half-away-from-zero, the project's established ledger rounding (ADR-115/119)."""


def quantize_quantity(value: Decimal) -> Decimal:
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_QUANTITY_QUANTUM, rounding=_ROUNDING)


def quantize_cents(value: Decimal) -> Decimal:
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_MONEY_QUANTUM, rounding=_ROUNDING)


# --------------------------------------------------------------------------------------
# Inputs (locked, persisted facts)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PurchasedLineFact:
    line_id: uuid.UUID
    product_id: uuid.UUID
    quantity: Decimal
    """The line's `underlying_quantity` (== its authoritative Purchased reservation)."""


@dataclass(frozen=True)
class PurchasedInventoryFact:
    product_id: uuid.UUID
    physical_quantity: Decimal
    weighted_average_unit_cost: Decimal
    """Read while the inventory row is locked (ADR-059)."""


@dataclass(frozen=True)
class SurplusAllocationFact:
    allocation_id: uuid.UUID
    lot_id: uuid.UUID
    line_id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True)
class SurplusLotFact:
    lot_id: uuid.UUID
    physical_quantity: Decimal
    unit_cost_basis: Decimal


@dataclass(frozen=True)
class LineSnapshotFact:
    line_id: uuid.UUID
    packaging_cost_total_snapshot: Decimal
    custom_direct_cost_estimate: Decimal | None


# --------------------------------------------------------------------------------------
# Plan (outputs)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PurchasedFulfillmentPlan:
    line_id: uuid.UUID
    product_id: uuid.UUID
    quantity: Decimal
    unit_cost: Decimal
    """The WAC snapshot, persisted on the transaction row."""
    total_cost: Decimal
    """`quantity x unit_cost`, quantized once to 6dp (the transaction's `total_cost`)."""
    ledger_amount: Decimal
    """The persisted `total_cost` quantized once to cents (the `PURCHASED_GOOD` amount)."""
    transaction_id: uuid.UUID = field(default_factory=uuid.uuid4)
    """Identity of the `ORDER_FULFILLMENT` inventory transaction row this plan creates, so
    the ledger row can reference it. Identity only — not part of any business value."""


@dataclass(frozen=True)
class SurplusConsumptionPlan:
    allocation_id: uuid.UUID
    lot_id: uuid.UUID
    line_id: uuid.UUID
    quantity: Decimal
    unit_cost_basis: Decimal
    ledger_amount: Decimal
    transaction_id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True)
class LineAmountPlan:
    line_id: uuid.UUID
    amount: Decimal


@dataclass(frozen=True)
class CompletionPlan:
    purchased: list[PurchasedFulfillmentPlan]
    purchased_deduction_by_product: dict[uuid.UUID, Decimal]
    surplus: list[SurplusConsumptionPlan]
    surplus_consumption_by_lot: dict[uuid.UUID, Decimal]
    packaging: list[LineAmountPlan]
    custom_direct: list[LineAmountPlan]

    def expected_ledger_total(self) -> Decimal:
        with decimal.localcontext() as ctx:
            ctx.prec = _CONTEXT_PRECISION
            return (
                sum((p.ledger_amount for p in self.purchased), Decimal(0))
                + sum((s.ledger_amount for s in self.surplus), Decimal(0))
                + sum((p.amount for p in self.packaging), Decimal(0))
                + sum((c.amount for c in self.custom_direct), Decimal(0))
            )


@dataclass(frozen=True)
class PlanFailure:
    code: str
    message: str
    details: dict[str, str]


# --------------------------------------------------------------------------------------
# Sufficiency (aggregated by Product identity / lot identity — never line-by-line)
# --------------------------------------------------------------------------------------


def check_physical_sufficiency(
    *,
    purchased_lines: Sequence[PurchasedLineFact],
    purchased_inventory: dict[uuid.UUID, PurchasedInventoryFact],
    surplus_allocations: Sequence[SurplusAllocationFact],
    surplus_lots: dict[uuid.UUID, SurplusLotFact],
) -> list[PlanFailure]:
    """Planned Completion must never drive a physical balance negative. Purchased demand
    is **aggregated per Product** (an Order may carry several Purchased lines for the same
    Product) and compared with the locked inventory; Surplus consumption is aggregated per
    lot. A missing inventory row or lot means zero stock."""
    failures: list[PlanFailure] = []
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION

        needed_by_product: dict[uuid.UUID, Decimal] = {}
        for line in purchased_lines:
            needed_by_product[line.product_id] = (
                needed_by_product.get(line.product_id, Decimal(0)) + line.quantity
            )
        for product_id in sorted(needed_by_product, key=str):
            needed = needed_by_product[product_id]
            inventory = purchased_inventory.get(product_id)
            physical = inventory.physical_quantity if inventory is not None else Decimal(0)
            if physical - needed < 0:
                failures.append(
                    PlanFailure(
                        "PURCHASED_STOCK_INSUFFICIENT",
                        "Purchased stock on hand no longer covers this order.",
                        {
                            "product_id": str(product_id),
                            "required_quantity": str(needed),
                            "physical_quantity": str(physical),
                        },
                    )
                )

        consumed_by_lot: dict[uuid.UUID, Decimal] = {}
        for allocation in surplus_allocations:
            consumed_by_lot[allocation.lot_id] = (
                consumed_by_lot.get(allocation.lot_id, Decimal(0)) + allocation.quantity
            )
        for lot_id in sorted(consumed_by_lot, key=str):
            lot = surplus_lots.get(lot_id)
            physical = lot.physical_quantity if lot is not None else Decimal(0)
            if physical - consumed_by_lot[lot_id] < 0:
                failures.append(
                    PlanFailure(
                        "SURPLUS_STOCK_INSUFFICIENT",
                        "A Surplus lot no longer holds the quantity promised to this order.",
                        {
                            "surplus_inventory_id": str(lot_id),
                            "required_quantity": str(consumed_by_lot[lot_id]),
                            "physical_quantity": str(physical),
                        },
                    )
                )
    return failures


# --------------------------------------------------------------------------------------
# Plan construction
# --------------------------------------------------------------------------------------


def build_completion_plan(
    *,
    purchased_lines: Sequence[PurchasedLineFact],
    purchased_inventory: dict[uuid.UUID, PurchasedInventoryFact],
    surplus_allocations: Sequence[SurplusAllocationFact],
    surplus_lots: dict[uuid.UUID, SurplusLotFact],
    line_snapshots: Sequence[LineSnapshotFact],
) -> tuple[CompletionPlan | None, list[PlanFailure]]:
    """Builds the exact Completion plan from locked persisted facts, or returns failures
    (insufficient stock, an unrepresentable derived value) without a plan. Deterministic:
    inputs are processed in a fixed order (line id, then lot id / allocation id)."""
    failures = check_physical_sufficiency(
        purchased_lines=purchased_lines,
        purchased_inventory=purchased_inventory,
        surplus_allocations=surplus_allocations,
        surplus_lots=surplus_lots,
    )
    if failures:
        return None, failures

    purchased_plans: list[PurchasedFulfillmentPlan] = []
    deduction: dict[uuid.UUID, Decimal] = {}
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        for line in sorted(purchased_lines, key=lambda fact: str(fact.line_id)):
            inventory = purchased_inventory[line.product_id]
            unit_cost = inventory.weighted_average_unit_cost
            total_cost = quantize_quantity(line.quantity * unit_cost)
            amount = quantize_cents(total_cost)
            if not (
                is_representable_in_numeric_18_6(total_cost)
                and is_representable_in_numeric_14_2(amount)
            ):
                return None, [
                    PlanFailure(
                        "COMPLETION_AMOUNT_NOT_REPRESENTABLE",
                        "A derived Purchased cost is too large to record.",
                        {"order_line_id": str(line.line_id)},
                    )
                ]
            purchased_plans.append(
                PurchasedFulfillmentPlan(
                    line_id=line.line_id,
                    product_id=line.product_id,
                    quantity=line.quantity,
                    unit_cost=unit_cost,
                    total_cost=total_cost,
                    ledger_amount=amount,
                )
            )
            deduction[line.product_id] = deduction.get(line.product_id, Decimal(0)) + line.quantity

        surplus_plans: list[SurplusConsumptionPlan] = []
        consumption: dict[uuid.UUID, Decimal] = {}
        for allocation in sorted(
            surplus_allocations,
            key=lambda a: (str(a.line_id), str(a.lot_id), str(a.allocation_id)),
        ):
            lot = surplus_lots[allocation.lot_id]
            amount = quantize_cents(allocation.quantity * lot.unit_cost_basis)
            if not is_representable_in_numeric_14_2(amount):
                return None, [
                    PlanFailure(
                        "COMPLETION_AMOUNT_NOT_REPRESENTABLE",
                        "A derived Surplus cost is too large to record.",
                        {"order_line_id": str(allocation.line_id)},
                    )
                ]
            surplus_plans.append(
                SurplusConsumptionPlan(
                    allocation_id=allocation.allocation_id,
                    lot_id=allocation.lot_id,
                    line_id=allocation.line_id,
                    quantity=allocation.quantity,
                    unit_cost_basis=lot.unit_cost_basis,
                    ledger_amount=amount,
                )
            )
            consumption[allocation.lot_id] = (
                consumption.get(allocation.lot_id, Decimal(0)) + allocation.quantity
            )

    ordered_snapshots = sorted(line_snapshots, key=lambda s: str(s.line_id))
    packaging = [
        LineAmountPlan(s.line_id, s.packaging_cost_total_snapshot) for s in ordered_snapshots
    ]
    custom_direct = [
        LineAmountPlan(s.line_id, s.custom_direct_cost_estimate)
        for s in ordered_snapshots
        if s.custom_direct_cost_estimate is not None
    ]
    return (
        CompletionPlan(
            purchased=purchased_plans,
            purchased_deduction_by_product=deduction,
            surplus=surplus_plans,
            surplus_consumption_by_lot=consumption,
            packaging=packaging,
            custom_direct=custom_direct,
        ),
        [],
    )
