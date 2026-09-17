"""Deterministic, line-level Surplus allocation (Spec §7.11/§14.5; Phase 7 Plan v2
§4/§8, corrected by Final Pre-Implementation Amendment §6 and Final Architecture Lock
§D).

Pure, side-effect-free: no HTTP or database access (ADR-109). Produces one
`SurplusAllocationResult` per (demand item, lot) pair actually consumed — a single
OrderLine's demand may legitimately span multiple lots, and a single lot may
legitimately serve multiple OrderLines. The caller is responsible for:

- passing only `remaining_allocatable_demand` per demand item (its raw
  `line_demand_quantity` minus any already-fixed/production-locked Surplus
  allocation for that same line — Final Architecture Lock §D), never the line's full
  demand quantity when part of it is already fixedly covered;
- passing each lot's `already_allocated_elsewhere` as the quantity already
  committed to demand this recalculation pass may not touch (an `IN_PRODUCTION`-
  covered `ProductionRequirement`, per Plan v2 §6) — never a different Product's
  allocations, since `SurplusInventory.product_id` scopes a lot to exactly one
  Product;
- aggregating the returned results back into `ProductionRequirement.surplus_allocated_quantity`
  together with the fixed portion that was deliberately excluded from this call.
"""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

_CONTEXT_PRECISION = 50


@dataclass(frozen=True)
class DemandItem:
    """One unit of produced-goods demand competing for existing Surplus — already
    reduced to `remaining_allocatable_demand` by the caller (see module docstring)."""

    order_id: uuid.UUID
    order_line_id: uuid.UUID  # stable tie-break identity (Spec §7.11)
    product_id: uuid.UUID
    recipe_revision_id: uuid.UUID | None
    quantity: Decimal
    demand_date: date
    fulfillment_time: time | None  # never fabricated when absent (Spec §14.12)


@dataclass(frozen=True)
class SurplusLot:
    surplus_inventory_id: uuid.UUID
    product_id: uuid.UUID
    physical_quantity: Decimal
    already_allocated_elsewhere: Decimal
    usable_through_date: date | None
    produced_at: datetime


@dataclass(frozen=True)
class SurplusAllocationResult:
    surplus_inventory_id: uuid.UUID
    order_id: uuid.UUID
    order_line_id: uuid.UUID
    product_id: uuid.UUID
    recipe_revision_id: uuid.UUID | None
    demand_date: date
    quantity: Decimal


def _demand_sort_key(item: DemandItem) -> tuple:
    """Earliest fulfillment date first, then earliest time (an item with no time
    sorts AFTER a same-day item that has one — never fabricated), then the stable
    `order_line_id` tie-break (Spec §7.11/§14.5)."""
    return (
        item.demand_date,
        item.fulfillment_time is None,
        item.fulfillment_time or time.min,
        item.order_line_id,
    )


def _lot_sort_key(lot: SurplusLot) -> tuple:
    """Earliest usable-through date first; null usable-through sorts LAST (Spec
    §7.11/§14.5 — prefer using dated stock before it expires; undated stock is the
    flexible backup); then oldest-produced-first; then a stable id tie-break."""
    return (
        lot.usable_through_date is None,
        lot.usable_through_date or date.min,
        lot.produced_at,
        lot.surplus_inventory_id,
    )


def _is_lot_eligible(lot: SurplusLot, demand_item: DemandItem, *, today: date) -> bool:
    """Spec §7.11 base rule (`usable_through_date IS NULL OR usable_through_date >=
    demand_date`), corrected by Plan v2 §2/§4: a lot that would satisfy the base rule
    for an overdue Order's demand date but has already expired as of `today` is
    excluded regardless — `usable_through_date >= max(demand_date, today)`."""
    if lot.product_id != demand_item.product_id:
        return False
    if lot.usable_through_date is None:
        return True
    return lot.usable_through_date >= max(demand_item.demand_date, today)


def allocate_surplus(
    lots: list[SurplusLot],
    demand_items: list[DemandItem],
    *,
    today: date,
) -> list[SurplusAllocationResult]:
    """Greedy, deterministic allocation: walk demand items in priority order; for
    each, walk remaining-eligible lots in their own priority order, consuming
    `min(remaining_demand, remaining_lot_quantity)` per lot until the item is fully
    satisfied or no eligible lots remain. All Decimal arithmetic runs inside an
    explicit, fixed-precision `decimal.localcontext()` (ADR-109) — never under the
    caller's ambient global context."""
    sorted_demand = sorted(demand_items, key=_demand_sort_key)
    sorted_lots = sorted(lots, key=_lot_sort_key)

    results: list[SurplusAllocationResult] = []
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        remaining_demand: dict[uuid.UUID, Decimal] = {
            item.order_line_id: item.quantity for item in demand_items
        }
        remaining_lot: dict[uuid.UUID, Decimal] = {
            lot.surplus_inventory_id: max(
                Decimal(0), lot.physical_quantity - lot.already_allocated_elsewhere
            )
            for lot in lots
        }

        for item in sorted_demand:
            need = remaining_demand[item.order_line_id]
            if need <= 0:
                continue
            for lot in sorted_lots:
                if need <= 0:
                    break
                if not _is_lot_eligible(lot, item, today=today):
                    continue
                available = remaining_lot[lot.surplus_inventory_id]
                if available <= 0:
                    continue
                consumed = min(need, available)
                if consumed <= 0:
                    continue
                results.append(
                    SurplusAllocationResult(
                        surplus_inventory_id=lot.surplus_inventory_id,
                        order_id=item.order_id,
                        order_line_id=item.order_line_id,
                        product_id=item.product_id,
                        recipe_revision_id=item.recipe_revision_id,
                        demand_date=item.demand_date,
                        quantity=consumed,
                    )
                )
                need -= consumed
                remaining_lot[lot.surplus_inventory_id] = available - consumed
            remaining_demand[item.order_line_id] = need

    return results
