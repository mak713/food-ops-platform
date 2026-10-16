"""Unit tests for the pure Shopping List calculators (Phase 9; ADR-130; approved range
rules: `start >= today`, `end >= start`, at most 90 calendar days inclusive)."""

from __future__ import annotations

import decimal
from datetime import date, timedelta
from decimal import Decimal

from app.domain import shopping_list as sl

TODAY = date(2026, 6, 1)


def _resolve(**overrides):
    kwargs = dict(
        business_today=TODAY,
        default_horizon_days=7,
        horizon_days=None,
        start_date=None,
        end_date=None,
    )
    kwargs.update(overrides)
    return sl.resolve_shopping_range(**kwargs)


# --------------------------------------------------------------------- range resolution


def test_default_preset_uses_business_horizon():
    resolved, issues = _resolve(default_horizon_days=3)
    assert issues == []
    assert resolved.horizon_days == 3 and not resolved.is_custom
    assert resolved.start_date == TODAY and resolved.end_date == TODAY + timedelta(days=3)
    assert len(resolved.display_dates()) == 4  # ADR-130: today .. today+N inclusive


def test_explicit_preset_overrides_default():
    resolved, _ = _resolve(horizon_days=7, default_horizon_days=3)
    assert resolved.end_date == TODAY + timedelta(days=7)
    assert len(resolved.display_dates()) == 8


def test_invalid_preset_is_rejected():
    resolved, issues = _resolve(horizon_days=5)
    assert resolved is None
    assert issues[0].code == "SHOPPING_LIST_HORIZON_INVALID"


def test_valid_custom_range():
    resolved, issues = _resolve(start_date=TODAY, end_date=TODAY + timedelta(days=10))
    assert issues == []
    assert resolved.is_custom and resolved.horizon_days is None
    assert len(resolved.display_dates()) == 11


def test_future_start_custom_range_allowed():
    resolved, issues = _resolve(
        start_date=TODAY + timedelta(days=5), end_date=TODAY + timedelta(days=8)
    )
    assert issues == [] and resolved.start_date == TODAY + timedelta(days=5)


def test_custom_range_start_in_past_rejected():
    resolved, issues = _resolve(start_date=TODAY - timedelta(days=1), end_date=TODAY)
    assert resolved is None
    assert [i.code for i in issues] == ["SHOPPING_LIST_RANGE_START_IN_PAST"]


def test_custom_range_end_before_start_rejected():
    resolved, issues = _resolve(
        start_date=TODAY + timedelta(days=3), end_date=TODAY + timedelta(days=2)
    )
    assert resolved is None
    assert [i.code for i in issues] == ["SHOPPING_LIST_RANGE_END_BEFORE_START"]


def test_custom_range_exactly_90_days_inclusive_allowed():
    resolved, issues = _resolve(start_date=TODAY, end_date=TODAY + timedelta(days=89))
    assert issues == [] and len(resolved.display_dates()) == 90


def test_custom_range_91_days_rejected():
    resolved, issues = _resolve(start_date=TODAY, end_date=TODAY + timedelta(days=90))
    assert resolved is None
    assert [i.code for i in issues] == ["SHOPPING_LIST_RANGE_TOO_LONG"]


def test_start_in_past_and_end_before_start_both_reported():
    _, issues = _resolve(start_date=TODAY - timedelta(days=2), end_date=TODAY - timedelta(days=3))
    assert {i.code for i in issues} == {
        "SHOPPING_LIST_RANGE_START_IN_PAST",
        "SHOPPING_LIST_RANGE_END_BEFORE_START",
    }


def test_half_specified_custom_range_rejected():
    for kw in ({"start_date": TODAY}, {"end_date": TODAY}):
        resolved, issues = _resolve(**kw)
        assert resolved is None
        assert issues[0].code == "SHOPPING_LIST_RANGE_INCOMPLETE"


def test_preset_and_custom_together_rejected():
    resolved, issues = _resolve(horizon_days=3, start_date=TODAY, end_date=TODAY)
    assert resolved is None
    assert issues[0].code == "SHOPPING_LIST_RANGE_CONFLICT"


# ----------------------------------------------------------------------- cumulative math


def _r(day_offset: int, qty: str):
    return (TODAY + timedelta(days=day_offset), Decimal(qty))


def test_no_double_counting_physical_is_subtracted_once():
    reservations = [_r(0, "60"), _r(2, "60")]
    assert sl.shortage_through(reservations, Decimal("100"), TODAY) == Decimal("0")
    assert sl.shortage_through(reservations, Decimal("100"), TODAY + timedelta(days=2)) == Decimal(
        "20"
    )


def test_overdue_reservation_counts_at_every_date():
    reservations = [_r(-12, "40"), _r(1, "80")]
    assert sl.shortage_through(reservations, Decimal("100"), TODAY + timedelta(days=1)) == Decimal(
        "20"
    )
    assert sl.shortage_through(reservations, Decimal("30"), TODAY) == Decimal("10")


def test_later_reservation_never_inflates_an_earlier_date():
    reservations = [_r(1, "5"), _r(19, "1000")]
    assert sl.shortage_through(reservations, Decimal("10"), TODAY + timedelta(days=1)) == Decimal(
        "0"
    )


def test_negative_physical_deepens_shortage():
    assert sl.shortage_through([_r(0, "10")], Decimal("-5"), TODAY) == Decimal("15")


def test_timeline_is_monotonic_and_ordered_by_display_dates():
    dates = [TODAY + timedelta(days=i) for i in range(5)]
    timeline = sl.build_timeline([_r(1, "30"), _r(3, "30")], Decimal("40"), dates)
    assert [t.demand_date for t in timeline] == dates
    shortages = [t.cumulative_shortage_through_date for t in timeline]
    assert shortages == [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("20"), Decimal("20")]
    assert shortages == sorted(shortages)


def test_first_shortage_date():
    dates = [TODAY + timedelta(days=i) for i in range(5)]
    timeline = sl.build_timeline([_r(2, "50")], Decimal("10"), dates)
    assert sl.first_shortage_date(timeline) == TODAY + timedelta(days=2)
    assert sl.first_shortage_date(sl.build_timeline([_r(2, "5")], Decimal("10"), dates)) is None


def test_unaffected_by_corrupted_ambient_decimal_context():
    original = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 2
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        result = sl.shortage_through([_r(0, "1234567.123456")], Decimal("0.000001"), TODAY)
        assert result == Decimal("1234567.123455")
    finally:
        decimal.setcontext(original)
