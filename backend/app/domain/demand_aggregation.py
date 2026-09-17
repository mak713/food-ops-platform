"""Deterministic demand-aggregation and shortage calculators (Spec §7.2/§14.3;
Phase 7 Plan v2 §4/§8).

Pure, side-effect-free: no HTTP or database access (ADR-109). All Decimal arithmetic
runs inside an explicit, fixed-precision `decimal.localcontext()`, matching the
established `order_pricing.py`/`inventory_costing.py` precedent exactly.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_CONTEXT_PRECISION = 50


@dataclass(frozen=True)
class ConfirmedProducedDemandLine:
    """One CONFIRMED OrderLine's contribution to produced-goods demand — already
    resolved to its currently-pinned RecipeRevision (or `None` for INCOMPLETE_RECIPE)
    by the caller (Plan v2 §9); this module only aggregates, it never resolves pins."""

    order_line_id: uuid.UUID
    product_id: uuid.UUID
    recipe_revision_id: uuid.UUID | None
    demand_date: date
    quantity: Decimal


DemandKey = tuple[uuid.UUID, "uuid.UUID | None", date]


def aggregate_produced_demand(
    lines: Sequence[ConfirmedProducedDemandLine],
) -> dict[DemandKey, Decimal]:
    """Groups confirmed produced-goods demand by `(product_id, recipe_revision_id,
    demand_date)` and sums quantity (Spec §14.3 aggregation key; §7.1's "aggregate
    before rounding" — this function performs no rounding at all, callers quantize
    once at the service layer per the Phase 5/6/7 persistence convention)."""
    totals: dict[DemandKey, Decimal] = {}
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        for line in lines:
            key = (line.product_id, line.recipe_revision_id, line.demand_date)
            totals[key] = totals.get(key, Decimal(0)) + line.quantity
    return totals


def calculate_shortage(required_or_reserved: Decimal, available: Decimal) -> Decimal:
    """`shortage = max(0, required_or_reserved - available)` (Spec §7.3/§14.9)."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return max(Decimal(0), required_or_reserved - available)


@dataclass(frozen=True)
class CustomItemWorkloadLine:
    """One CONFIRMED CUSTOM_ITEM OrderLine's manual workload contribution (ORD-010).
    Never aggregated into a ProductionRequirement — `product_id` is always `NULL` for
    a Custom Item line, and `production_requirements.product_id` is `NOT NULL`, so no
    table row can represent this; it exists only in an in-memory recalculation/preview
    result."""

    order_id: uuid.UUID
    order_line_id: uuid.UUID
    demand_date: date
    custom_active_time_minutes: int


@dataclass(frozen=True)
class CustomItemWorkloadItem:
    demand_date: date
    total_active_minutes: int
    contributing_order_line_ids: tuple[uuid.UUID, ...]


def aggregate_custom_item_workload(
    lines: Sequence[CustomItemWorkloadLine],
) -> list[CustomItemWorkloadItem]:
    """Groups Custom Item manual workload by `demand_date` only — a plain sum, kept
    structurally separate from batch-derived produced-goods workload (Plan v2 §4/§11).
    Deterministic: dates ascending, contributing lines sorted by `order_line_id`."""
    grouped: dict[date, list[CustomItemWorkloadLine]] = {}
    for line in lines:
        grouped.setdefault(line.demand_date, []).append(line)

    results: list[CustomItemWorkloadItem] = []
    for demand_date in sorted(grouped):
        group = sorted(grouped[demand_date], key=lambda item: item.order_line_id)
        total_minutes = sum((item.custom_active_time_minutes for item in group), 0)
        results.append(
            CustomItemWorkloadItem(
                demand_date=demand_date,
                total_active_minutes=total_minutes,
                contributing_order_line_ids=tuple(item.order_line_id for item in group),
            )
        )
    return results
