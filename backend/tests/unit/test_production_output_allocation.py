"""Unit tests for the pure production-output allocator (Phase 8 Final Architecture
Lock §7 / Final Architecture Amendment §1)."""

from __future__ import annotations

import uuid
from datetime import date, time
from decimal import Decimal

from app.domain.production_output_allocation import (
    OutstandingDemandLine,
    allocate_production_output,
    eligible_remaining_quantity,
)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def _line(
    *,
    product_id,
    underlying_quantity,
    completed=0,
    active_surplus=0,
    fulfillment_date=date(2026, 3, 5),
    fulfillment_time=None,
    order_line_id=None,
):
    return OutstandingDemandLine(
        order_id=_uid(),
        order_line_id=order_line_id or _uid(),
        product_id=product_id,
        underlying_quantity=Decimal(underlying_quantity),
        completed_allocation_quantity=Decimal(completed),
        active_surplus_allocation_quantity=Decimal(active_surplus),
        fulfillment_date=fulfillment_date,
        fulfillment_time=fulfillment_time,
    )


def test_eligible_remaining_quantity_nets_out_both_prior_sources():
    line = _line(product_id=_uid(), underlying_quantity=10, completed=3, active_surplus=2)
    assert eligible_remaining_quantity(line) == Decimal("5")


def test_eligible_remaining_quantity_never_negative():
    """A READY line (by definition fully satisfied, possibly by more than one
    source in combination) must never contribute negative — and therefore never
    positive — demand."""
    line = _line(product_id=_uid(), underlying_quantity=10, completed=7, active_surplus=6)
    assert eligible_remaining_quantity(line) == Decimal("0")


def test_fully_satisfied_ready_line_contributes_zero_new_demand():
    product_id = _uid()
    ready_line = _line(product_id=product_id, underlying_quantity=10, completed=10)
    outcome = allocate_production_output([ready_line], Decimal("50"), product_id=product_id)
    assert outcome.allocations == []
    assert outcome.remaining_excess == Decimal("50")


def test_full_allocation_to_single_line():
    product_id = _uid()
    line = _line(product_id=product_id, underlying_quantity=6)
    outcome = allocate_production_output([line], Decimal("10"), product_id=product_id)
    assert len(outcome.allocations) == 1
    assert outcome.allocations[0].quantity == Decimal("6")
    assert outcome.allocations[0].order_line_id == line.order_line_id
    assert outcome.remaining_excess == Decimal("4")


def test_output_split_across_multiple_lines_by_priority():
    product_id = _uid()
    earlier = _line(product_id=product_id, underlying_quantity=5, fulfillment_date=date(2026, 3, 1))
    later = _line(product_id=product_id, underlying_quantity=5, fulfillment_date=date(2026, 3, 10))
    outcome = allocate_production_output([later, earlier], Decimal("7"), product_id=product_id)
    by_line = {r.order_line_id: r.quantity for r in outcome.allocations}
    assert by_line[earlier.order_line_id] == Decimal("5")
    assert by_line[later.order_line_id] == Decimal("2")
    assert outcome.remaining_excess == Decimal("0")


def test_insufficient_output_satisfies_only_earliest_deadline_first():
    product_id = _uid()
    earlier = _line(product_id=product_id, underlying_quantity=5, fulfillment_date=date(2026, 3, 1))
    later = _line(product_id=product_id, underlying_quantity=5, fulfillment_date=date(2026, 3, 10))
    outcome = allocate_production_output([earlier, later], Decimal("3"), product_id=product_id)
    assert len(outcome.allocations) == 1
    assert outcome.allocations[0].order_line_id == earlier.order_line_id
    assert outcome.allocations[0].quantity == Decimal("3")
    assert outcome.remaining_excess == Decimal("0")


def test_missing_fulfillment_time_sorts_after_same_day_timed_line():
    product_id = _uid()
    same_day = date(2026, 3, 5)
    timed = _line(
        product_id=product_id,
        underlying_quantity=1,
        fulfillment_date=same_day,
        fulfillment_time=time(9, 0),
    )
    untimed = _line(
        product_id=product_id,
        underlying_quantity=1,
        fulfillment_date=same_day,
        fulfillment_time=None,
    )
    outcome = allocate_production_output([untimed, timed], Decimal("1"), product_id=product_id)
    assert len(outcome.allocations) == 1
    assert outcome.allocations[0].order_line_id == timed.order_line_id


def test_stable_order_line_id_tiebreak_is_deterministic():
    product_id = _uid()
    same_day = date(2026, 3, 5)
    line_a = _line(
        product_id=product_id,
        underlying_quantity=1,
        fulfillment_date=same_day,
        order_line_id=uuid.UUID(int=1),
    )
    line_b = _line(
        product_id=product_id,
        underlying_quantity=1,
        fulfillment_date=same_day,
        order_line_id=uuid.UUID(int=2),
    )
    outcome_1 = allocate_production_output([line_b, line_a], Decimal("1"), product_id=product_id)
    outcome_2 = allocate_production_output([line_a, line_b], Decimal("1"), product_id=product_id)
    assert outcome_1.allocations[0].order_line_id == line_a.order_line_id
    assert outcome_2.allocations[0].order_line_id == line_a.order_line_id


def test_other_products_demand_never_participates():
    product_id = _uid()
    other_product_id = _uid()
    own_line = _line(product_id=product_id, underlying_quantity=3)
    other_line = _line(product_id=other_product_id, underlying_quantity=100)
    outcome = allocate_production_output(
        [own_line, other_line], Decimal("10"), product_id=product_id
    )
    assert len(outcome.allocations) == 1
    assert outcome.allocations[0].order_line_id == own_line.order_line_id
    assert outcome.remaining_excess == Decimal("7")


def test_zero_output_produces_no_allocations():
    product_id = _uid()
    line = _line(product_id=product_id, underlying_quantity=5)
    outcome = allocate_production_output([line], Decimal("0"), product_id=product_id)
    assert outcome.allocations == []
    assert outcome.remaining_excess == Decimal("0")


def test_no_demand_lines_leaves_entire_output_as_excess():
    product_id = _uid()
    outcome = allocate_production_output([], Decimal("12"), product_id=product_id)
    assert outcome.allocations == []
    assert outcome.remaining_excess == Decimal("12")


def test_partial_prior_production_leaves_only_genuinely_remaining_quantity():
    """A still-CONFIRMED line with an earlier shortfall run's partial completion
    already allocated must only surface its genuinely remaining quantity to a
    follow-up ('Continue Production') run's allocation pass."""
    product_id = _uid()
    line = _line(product_id=product_id, underlying_quantity=10, completed=4)
    outcome = allocate_production_output([line], Decimal("10"), product_id=product_id)
    assert len(outcome.allocations) == 1
    assert outcome.allocations[0].quantity == Decimal("6")
    assert outcome.remaining_excess == Decimal("4")
