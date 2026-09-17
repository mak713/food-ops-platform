"""Unit tests for demand aggregation and shortage (Phase 7 Plan v2 §4)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.domain.demand_aggregation import (
    ConfirmedProducedDemandLine,
    CustomItemWorkloadLine,
    aggregate_custom_item_workload,
    aggregate_produced_demand,
    calculate_shortage,
)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def test_aggregate_produced_demand_sums_within_one_key():
    product_id = _uid()
    revision_id = _uid()
    day = date(2026, 3, 5)
    lines = [
        ConfirmedProducedDemandLine(_uid(), product_id, revision_id, day, Decimal("6")),
        ConfirmedProducedDemandLine(_uid(), product_id, revision_id, day, Decimal("6")),
    ]
    totals = aggregate_produced_demand(lines)
    assert totals[(product_id, revision_id, day)] == Decimal("12")


def test_aggregate_produced_demand_never_merges_different_revisions():
    product_id = _uid()
    revision_a, revision_b = _uid(), _uid()
    day = date(2026, 3, 5)
    lines = [
        ConfirmedProducedDemandLine(_uid(), product_id, revision_a, day, Decimal("6")),
        ConfirmedProducedDemandLine(_uid(), product_id, revision_b, day, Decimal("6")),
    ]
    totals = aggregate_produced_demand(lines)
    assert len(totals) == 2
    assert totals[(product_id, revision_a, day)] == Decimal("6")
    assert totals[(product_id, revision_b, day)] == Decimal("6")


def test_aggregate_produced_demand_incomplete_recipe_key_has_none_revision():
    product_id = _uid()
    day = date(2026, 3, 5)
    lines = [ConfirmedProducedDemandLine(_uid(), product_id, None, day, Decimal("6"))]
    totals = aggregate_produced_demand(lines)
    assert totals[(product_id, None, day)] == Decimal("6")


def test_calculate_shortage_non_negative():
    assert calculate_shortage(Decimal("10"), Decimal("15")) == Decimal("0")
    assert calculate_shortage(Decimal("10"), Decimal("4")) == Decimal("6")
    assert calculate_shortage(Decimal("10"), Decimal("10")) == Decimal("0")


def test_custom_item_workload_grouped_by_date_and_summed():
    day1, day2 = date(2026, 3, 5), date(2026, 3, 6)
    line_a, line_b, line_c = _uid(), _uid(), _uid()
    lines = [
        CustomItemWorkloadLine(_uid(), line_a, day1, 30),
        CustomItemWorkloadLine(_uid(), line_b, day1, 15),
        CustomItemWorkloadLine(_uid(), line_c, day2, 20),
    ]
    result = aggregate_custom_item_workload(lines)
    assert [item.demand_date for item in result] == [day1, day2]
    assert result[0].total_active_minutes == 45
    assert result[1].total_active_minutes == 20


def test_custom_item_workload_deterministic_line_ordering():
    day = date(2026, 3, 5)
    line_hi, line_lo = _uid(), _uid()
    while line_hi < line_lo:
        line_hi, line_lo = _uid(), _uid()
    lines = [
        CustomItemWorkloadLine(_uid(), line_hi, day, 10),
        CustomItemWorkloadLine(_uid(), line_lo, day, 5),
    ]
    result = aggregate_custom_item_workload(lines)
    assert result[0].contributing_order_line_ids == (line_lo, line_hi)
