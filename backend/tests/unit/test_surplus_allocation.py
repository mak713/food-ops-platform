"""Unit tests for the line-level Surplus allocator (Phase 7 Plan v2 §4, corrected by
Final Pre-Implementation Amendment §6 and Final Architecture Lock §D)."""

from __future__ import annotations

import decimal
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from app.domain.surplus_allocation import DemandItem, SurplusLot, allocate_surplus


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def _demand(*, product_id, quantity, demand_date, fulfillment_time=None, order_line_id=None):
    return DemandItem(
        order_id=_uid(),
        order_line_id=order_line_id or _uid(),
        product_id=product_id,
        recipe_revision_id=_uid(),
        quantity=Decimal(quantity),
        demand_date=demand_date,
        fulfillment_time=fulfillment_time,
    )


def _lot(*, product_id, physical_quantity, usable_through_date=None, produced_at=None, already=0):
    return SurplusLot(
        surplus_inventory_id=_uid(),
        product_id=product_id,
        physical_quantity=Decimal(physical_quantity),
        already_allocated_elsewhere=Decimal(already),
        usable_through_date=usable_through_date,
        produced_at=produced_at or datetime(2026, 1, 1),
    )


def test_full_allocation_from_single_lot():
    product_id = _uid()
    lot = _lot(product_id=product_id, physical_quantity=10)
    demand = _demand(product_id=product_id, quantity=6, demand_date=date(2026, 3, 5))
    results = allocate_surplus([lot], [demand], today=date(2026, 3, 1))
    assert len(results) == 1
    assert results[0].quantity == Decimal("6")
    assert results[0].surplus_inventory_id == lot.surplus_inventory_id


def test_demand_spans_multiple_lots_when_one_is_insufficient():
    product_id = _uid()
    lot_a = _lot(product_id=product_id, physical_quantity=4, produced_at=datetime(2026, 1, 1))
    lot_b = _lot(product_id=product_id, physical_quantity=10, produced_at=datetime(2026, 1, 2))
    demand = _demand(product_id=product_id, quantity=6, demand_date=date(2026, 3, 5))
    results = allocate_surplus([lot_a, lot_b], [demand], today=date(2026, 3, 1))
    total = sum((r.quantity for r in results), Decimal(0))
    assert total == Decimal("6")
    assert {r.surplus_inventory_id for r in results} == {
        lot_a.surplus_inventory_id,
        lot_b.surplus_inventory_id,
    }


def test_earliest_usable_through_date_consumed_first_null_sorts_last():
    product_id = _uid()
    dated_lot = _lot(
        product_id=product_id, physical_quantity=3, usable_through_date=date(2026, 3, 10)
    )
    undated_lot = _lot(product_id=product_id, physical_quantity=10, usable_through_date=None)
    demand = _demand(product_id=product_id, quantity=3, demand_date=date(2026, 3, 5))
    results = allocate_surplus([undated_lot, dated_lot], [demand], today=date(2026, 3, 1))
    assert len(results) == 1
    assert results[0].surplus_inventory_id == dated_lot.surplus_inventory_id


def test_lot_expired_as_of_today_excluded_even_for_overdue_demand():
    """Final Pre-Implementation Amendment §2 / Final Architecture Lock: a lot whose
    usable_through_date satisfies the base 'on/after demand_date' rule for an overdue
    Order is still excluded if it has already expired as of the current Business-local
    date."""
    product_id = _uid()
    overdue_demand_date = date(2026, 1, 1)
    today = date(2026, 1, 15)
    lot = _lot(
        product_id=product_id,
        physical_quantity=10,
        usable_through_date=date(2026, 1, 5),  # >= demand_date, but < today
    )
    demand = _demand(product_id=product_id, quantity=3, demand_date=overdue_demand_date)
    results = allocate_surplus([lot], [demand], today=today)
    assert results == []


def test_lot_still_eligible_when_usable_through_on_or_after_today():
    product_id = _uid()
    today = date(2026, 1, 15)
    lot = _lot(product_id=product_id, physical_quantity=10, usable_through_date=today)
    demand = _demand(product_id=product_id, quantity=3, demand_date=date(2026, 1, 1))
    results = allocate_surplus([lot], [demand], today=today)
    assert len(results) == 1
    assert results[0].quantity == Decimal("3")


def test_demand_priority_earliest_date_then_time_then_line_id():
    product_id = _uid()
    lot = _lot(product_id=product_id, physical_quantity=5)
    line_early_day, line_late_day = _uid(), _uid()
    while line_early_day < line_late_day:
        line_early_day, line_late_day = _uid(), _uid()
    demand_late_day = _demand(
        product_id=product_id,
        quantity=5,
        demand_date=date(2026, 3, 6),
        order_line_id=line_early_day,
    )
    demand_early_day = _demand(
        product_id=product_id,
        quantity=5,
        demand_date=date(2026, 3, 5),
        order_line_id=line_late_day,
    )
    results = allocate_surplus([lot], [demand_late_day, demand_early_day], today=date(2026, 3, 1))
    # Only 5 units of lot exist; the earlier demand_date must win regardless of
    # order_line_id ordering.
    assert len(results) == 1
    assert results[0].order_line_id == line_late_day  # the earlier-date demand item


def test_missing_time_sorts_after_a_same_day_item_with_a_time():
    product_id = _uid()
    day = date(2026, 3, 5)
    lot = _lot(product_id=product_id, physical_quantity=5)
    line_no_time, line_with_time = _uid(), _uid()
    demand_no_time = _demand(
        product_id=product_id,
        quantity=5,
        demand_date=day,
        fulfillment_time=None,
        order_line_id=line_no_time,
    )
    demand_with_time = _demand(
        product_id=product_id,
        quantity=5,
        demand_date=day,
        fulfillment_time=time(9, 0),
        order_line_id=line_with_time,
    )
    results = allocate_surplus([lot], [demand_no_time, demand_with_time], today=date(2026, 3, 1))
    assert len(results) == 1
    assert results[0].order_line_id == line_with_time


def test_lot_scoped_to_its_own_product_only():
    product_a, product_b = _uid(), _uid()
    lot = _lot(product_id=product_a, physical_quantity=10)
    demand = _demand(product_id=product_b, quantity=5, demand_date=date(2026, 3, 5))
    results = allocate_surplus([lot], [demand], today=date(2026, 3, 1))
    assert results == []


def test_already_allocated_elsewhere_reduces_available_quantity():
    product_id = _uid()
    lot = _lot(product_id=product_id, physical_quantity=10, already=7)
    demand = _demand(product_id=product_id, quantity=5, demand_date=date(2026, 3, 5))
    results = allocate_surplus([lot], [demand], today=date(2026, 3, 1))
    assert len(results) == 1
    assert results[0].quantity == Decimal("3")


def test_allocation_result_unaffected_by_corrupted_ambient_decimal_context():
    """ADR-109: `allocate_surplus` must run its own arithmetic inside an isolated
    `decimal.localcontext()`, never the caller's ambient global context. Prove this by
    deliberately corrupting the global context (very low precision, and a non-default
    rounding mode) immediately before calling, and asserting the result is bit-for-bit
    identical to the same call under a pristine ambient context — a caller that merely
    happens to have a low-precision context active elsewhere (e.g. another library)
    must never silently corrupt a Phase 7 operational calculation."""
    product_id = _uid()
    lot_a = _lot(product_id=product_id, physical_quantity=Decimal("12345.123456"))
    lot_b = _lot(
        product_id=product_id,
        physical_quantity=Decimal("6789.654321"),
        produced_at=datetime(2026, 1, 2),
    )
    demand = _demand(
        product_id=product_id, quantity=Decimal("19134.777776"), demand_date=date(2026, 3, 5)
    )

    baseline = allocate_surplus([lot_a, lot_b], [demand], today=date(2026, 3, 1))

    original_ctx = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 2
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        corrupted = allocate_surplus([lot_a, lot_b], [demand], today=date(2026, 3, 1))
    finally:
        decimal.setcontext(original_ctx)

    assert [(r.surplus_inventory_id, r.quantity) for r in baseline] == [
        (r.surplus_inventory_id, r.quantity) for r in corrupted
    ]
    total = sum((r.quantity for r in corrupted), Decimal(0))
    assert total == Decimal("19134.777776")
