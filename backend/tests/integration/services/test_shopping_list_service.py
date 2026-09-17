"""Integration tests for the Shopping List cumulative-horizon derivation (Final
Pre-Implementation Amendment §1's three worked examples, verbatim)."""

from __future__ import annotations

import decimal
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.enums import CalculationStatus, ProductType
from app.services.shopping_list_service import derive_shopping_list
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_ingredient_reservation,
    make_product,
    make_production_requirement,
)

_TODAY = date(2026, 6, 1)


def _reservation(session, business, product, ingredient, *, demand_date, quantity):
    requirement = make_production_requirement(
        session,
        business,
        product,
        None,
        demand_date=demand_date,
        calculation_status=CalculationStatus.INCOMPLETE_RECIPE,
        confirmed_demand_quantity=quantity,
        production_demand_quantity=quantity,
    )
    session.flush()
    make_ingredient_reservation(
        session, business, requirement, ingredient, quantity_canonical=quantity
    )


def test_no_double_counting_day1_zero_day3_twenty(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100")

    _reservation(session, business, product, ingredient, demand_date=_TODAY, quantity=Decimal("60"))
    _reservation(
        session,
        business,
        product,
        ingredient,
        demand_date=date(2026, 6, 3),
        quantity=Decimal("60"),
    )
    session.commit()

    results = derive_shopping_list(session, business, business_today=_TODAY, horizon_days=7)
    by_date = {d.demand_date: d for d in results[0].by_date}

    assert by_date[_TODAY].cumulative_shortage_through_date == Decimal("0")
    assert by_date[date(2026, 6, 3)].cumulative_shortage_through_date == Decimal("20")


def test_overdue_reservation_precedes_tomorrow_demand(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100")

    overdue_date = date(2026, 5, 20)  # before business_today
    tomorrow = date(2026, 6, 2)
    _reservation(
        session, business, product, ingredient, demand_date=overdue_date, quantity=Decimal("40")
    )
    _reservation(
        session, business, product, ingredient, demand_date=tomorrow, quantity=Decimal("80")
    )
    session.commit()

    results = derive_shopping_list(session, business, business_today=_TODAY, horizon_days=7)
    by_date = {d.demand_date: d for d in results[0].by_date}

    assert by_date[tomorrow].cumulative_shortage_through_date == Decimal("20")


def test_reservation_beyond_horizon_does_not_inflate_result(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10")

    within_horizon = date(2026, 6, 2)
    beyond_horizon = date(2026, 6, 20)  # well past a 3-day horizon
    _reservation(
        session, business, product, ingredient, demand_date=within_horizon, quantity=Decimal("5")
    )
    _reservation(
        session, business, product, ingredient, demand_date=beyond_horizon, quantity=Decimal("1000")
    )
    session.commit()

    results = derive_shopping_list(session, business, business_today=_TODAY, horizon_days=3)
    by_date = {d.demand_date: d for d in results[0].by_date}

    assert within_horizon in by_date
    assert beyond_horizon not in by_date
    assert by_date[within_horizon].cumulative_shortage_through_date == Decimal("0")


def test_derive_shopping_list_unaffected_by_corrupted_ambient_decimal_context(session: Session):
    """ADR-109, service-layer coverage (authorized alongside the Phase 7 Final
    Semantic & Precision Correction Plan) — `derive_shopping_list`'s own
    cumulative-summation `decimal.localcontext()` must be genuinely isolated
    from the caller's ambient global Decimal context. Mirrors the existing
    domain-layer ADR-109 regression test (`test_surplus_allocation.
    test_allocation_result_unaffected_by_corrupted_ambient_decimal_context`):
    deliberately corrupt the global context (very low precision, a non-default
    rounding mode) immediately before calling, and assert the result is
    bit-for-bit identical to a pristine-context call."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100.123456")

    _reservation(
        session, business, product, ingredient, demand_date=_TODAY, quantity=Decimal("33.333333")
    )
    _reservation(
        session,
        business,
        product,
        ingredient,
        demand_date=date(2026, 6, 3),
        quantity=Decimal("66.666667"),
    )
    session.commit()

    baseline = derive_shopping_list(session, business, business_today=_TODAY, horizon_days=7)

    original_ctx = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 2
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        corrupted = derive_shopping_list(session, business, business_today=_TODAY, horizon_days=7)
    finally:
        decimal.setcontext(original_ctx)

    assert baseline == corrupted
