"""Decimal precision round-trip tests (Spec §7: never binary floating point).

NUMERIC(18,6) quantity columns and NUMERIC(14,2) money columns must round-trip
through Postgres as exact Decimal values, not lossy floats.
"""

from decimal import Decimal

from tests.integration.schema import factories as f


def test_quantity_column_round_trips_as_exact_decimal(session):
    business = f.make_business_graph(session)
    session.flush()
    ingredient = f.make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("123.456789")
    session.flush()
    session.refresh(ingredient)

    assert ingredient.physical_quantity == Decimal("123.456789")
    assert isinstance(ingredient.physical_quantity, Decimal)


def test_money_column_round_trips_as_exact_decimal(session):
    business = f.make_business_graph(session)
    session.flush()
    order = f.make_order(session, business)
    order.final_total = Decimal("1999.99")
    session.flush()
    session.refresh(order)

    assert order.final_total == Decimal("1999.99")
    assert isinstance(order.final_total, Decimal)


def test_ratio_column_round_trips_as_exact_decimal(session):
    business = f.make_business_graph(session)
    business.target_contribution_margin = Decimal("0.6")
    session.flush()
    session.refresh(business)

    assert business.target_contribution_margin == Decimal("0.600000")
