"""Pure tests for the Analytics KPI calculators (Phase 10; Spec §13.3)."""

from __future__ import annotations

from decimal import Decimal

from app.domain.analytics_kpis import (
    compare_kpis,
    compute_kpis,
    count_delta,
    money_delta,
    rate_delta,
)

D = Decimal


def test_basic_kpis():
    k = compute_kpis(revenue=D("200.00"), direct_cost=D("80.00"), completed_orders=3)
    assert k.revenue == D("200.00")
    assert k.direct_cost == D("80.00")
    assert k.contribution == D("120.00")
    assert k.contribution_margin == D("0.600000")
    assert k.completed_orders == 3
    assert k.average_order_value == D("66.67")  # 66.666.. ROUND_HALF_UP


def test_zero_revenue_margin_is_null_not_division_error():
    k = compute_kpis(revenue=D("0"), direct_cost=D("0"), completed_orders=0)
    assert k.contribution == D("0.00")
    assert k.contribution_margin is None
    assert k.average_order_value is None


def test_zero_revenue_with_orders_has_zero_aov_and_null_margin():
    k = compute_kpis(revenue=D("0"), direct_cost=D("2.50"), completed_orders=2)
    assert k.contribution == D("-2.50")
    assert k.contribution_margin is None
    assert k.average_order_value == D("0.00")


def test_negative_contribution_and_margin_are_allowed():
    k = compute_kpis(revenue=D("50.00"), direct_cost=D("75.00"), completed_orders=1)
    assert k.contribution == D("-25.00")
    assert k.contribution_margin == D("-0.500000")


def test_zero_cost_is_valid_fact():
    k = compute_kpis(revenue=D("10.00"), direct_cost=D("0"), completed_orders=1)
    assert k.contribution == D("10.00")
    assert k.contribution_margin == D("1.000000")


def test_rounding_half_up_at_boundaries():
    assert compute_kpis(
        revenue=D("0.05"), direct_cost=D("0"), completed_orders=2
    ).average_order_value == D("0.03")  # 0.025 -> 0.03
    k = compute_kpis(revenue=D("3.00"), direct_cost=D("1.00"), completed_orders=1)
    assert k.contribution_margin == D("0.666667")


def test_money_delta_percent_and_absolute():
    d = money_delta(D("150.00"), D("100.00"))
    assert d.absolute == D("50.00")
    assert d.percent_change == D("0.500000")
    d = money_delta(D("50.00"), D("100.00"))
    assert d.absolute == D("-50.00")
    assert d.percent_change == D("-0.500000")


def test_percent_change_null_when_previous_zero_negative_or_missing():
    assert money_delta(D("10.00"), D("0.00")).percent_change is None
    assert money_delta(D("10.00"), D("0.00")).absolute == D("10.00")
    assert money_delta(D("10.00"), D("-5.00")).percent_change is None
    assert money_delta(D("10.00"), None) == money_delta(None, D("1.00"))
    assert money_delta(None, None).absolute is None


def test_count_delta_stays_an_integer_count():
    d = count_delta(5, 4)
    assert d.absolute == 1 and isinstance(d.absolute, int)
    assert d.percent_change == D("0.250000")
    z = count_delta(3, 0)
    assert z.absolute == 3 and z.percent_change is None


def test_margin_delta_is_percentage_points_not_growth():
    d = rate_delta(D("0.600000"), D("0.500000"))
    assert d.absolute == D("0.100000")
    assert rate_delta(None, D("0.5")).absolute is None
    assert rate_delta(D("0.5"), None).absolute is None


def test_compare_kpis_with_no_prior_data():
    cur = compute_kpis(revenue=D("100.00"), direct_cost=D("40.00"), completed_orders=2)
    prev = compute_kpis(revenue=D("0"), direct_cost=D("0"), completed_orders=0)
    deltas = compare_kpis(cur, prev)
    assert deltas.revenue.absolute == D("100.00")
    assert deltas.revenue.percent_change is None
    assert deltas.contribution_margin.absolute is None  # previous margin undefined
    assert deltas.average_order_value.absolute is None
    assert deltas.completed_orders.absolute == 2
    assert deltas.completed_orders.percent_change is None
