"""Deterministic allocation of a completed Production Run's actual usable output to
outstanding demand (RUN-011; Phase 8 Final Architecture Lock §7 / Final Architecture
Amendment §1).

Pure, side-effect-free: no HTTP or database access (ADR-109), mirroring
`domain/surplus_allocation.py`'s established shape. Allocates *produced output* to
*demand* rather than *surplus lots* to *demand* — a single Production Run's output may
legitimately satisfy multiple OrderLines, but (unlike Surplus, which can draw from many
lots) a run's output is one undivided pool consumed strictly in priority order until
either the pool or the demand is exhausted.

The caller is responsible for:

- restricting `demand_lines` to the run's own `product_id` and to `CONFIRMED`/`READY`
  Order lines only (Phase 8 Final Architecture Lock §A) — allocation is Product-wide,
  not RecipeRevision-restricted (`SurplusInventory` itself carries no
  `recipe_revision_id`, and RUN-011 does not scope by revision either);
- supplying each line's `completed_allocation_quantity` (the sum of every prior,
  immutable `ProductionRunOrderAllocation` against this line) and
  `active_surplus_allocation_quantity` (the sum of every currently-active
  `SurplusAllocation` against this line) so `eligible_remaining_quantity` never
  double-satisfies demand already covered by either source.
"""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

_CONTEXT_PRECISION = 50


@dataclass(frozen=True)
class OutstandingDemandLine:
    """One OrderLine's still-open produced-demand, before this run's output is
    considered. `completed_allocation_quantity` and `active_surplus_allocation_quantity`
    are the caller's already-queried, already-summed totals from prior runs/lots — this
    module never re-derives them."""

    order_id: uuid.UUID
    order_line_id: uuid.UUID
    product_id: uuid.UUID
    underlying_quantity: Decimal
    completed_allocation_quantity: Decimal
    active_surplus_allocation_quantity: Decimal
    fulfillment_date: date
    fulfillment_time: time | None  # never fabricated when absent


@dataclass(frozen=True)
class ProductionOutputAllocationResult:
    order_id: uuid.UUID
    order_line_id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True)
class ProductionOutputAllocationOutcome:
    allocations: list[ProductionOutputAllocationResult]
    remaining_excess: Decimal


def eligible_remaining_quantity(line: OutstandingDemandLine) -> Decimal:
    """`max(0, underlying_quantity - completed_allocation_quantity -
    active_surplus_allocation_quantity)` (Phase 8 Final Architecture Lock §7) — the
    exact quantity of this line's demand not already satisfied by a prior run's
    immutable output allocation or by a currently-active Surplus reservation. A READY
    line (by definition fully satisfied) always nets to zero here, which is what
    prevents a completed line from ever re-entering new production demand."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return max(
            Decimal(0),
            line.underlying_quantity
            - line.completed_allocation_quantity
            - line.active_surplus_allocation_quantity,
        )


def _demand_sort_key(line: OutstandingDemandLine) -> tuple:
    """Earliest fulfillment date first, then earliest time (a line with no time sorts
    AFTER a same-day line that has one — never fabricated), then the stable
    `order_line_id` tie-break (RUN-011)."""
    return (
        line.fulfillment_date,
        line.fulfillment_time is None,
        line.fulfillment_time or time.min,
        line.order_line_id,
    )


def allocate_production_output(
    demand_lines: list[OutstandingDemandLine],
    actual_output_quantity: Decimal,
    *,
    product_id: uuid.UUID,
) -> ProductionOutputAllocationOutcome:
    """Walk `demand_lines` restricted to `product_id` in earliest-fulfillment-deadline
    order, consuming `min(remaining_output, eligible_remaining_quantity(line))` from
    the run's single output pool per line until either the pool or all demand is
    exhausted. All Decimal arithmetic runs inside an explicit, fixed-precision
    `decimal.localcontext()` (ADR-109), never under the caller's ambient global
    context. Any output left unconsumed after every eligible line has been walked is
    returned as `remaining_excess`, destined to become Surplus."""
    sorted_lines = sorted(
        (line for line in demand_lines if line.product_id == product_id),
        key=_demand_sort_key,
    )

    results: list[ProductionOutputAllocationResult] = []
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        remaining_output = actual_output_quantity

        for line in sorted_lines:
            if remaining_output <= 0:
                break
            need = eligible_remaining_quantity(line)
            if need <= 0:
                continue
            consumed = min(need, remaining_output)
            if consumed <= 0:
                continue
            results.append(
                ProductionOutputAllocationResult(
                    order_id=line.order_id,
                    order_line_id=line.order_line_id,
                    quantity=consumed,
                )
            )
            remaining_output -= consumed

        return ProductionOutputAllocationOutcome(
            allocations=results, remaining_excess=remaining_output
        )
