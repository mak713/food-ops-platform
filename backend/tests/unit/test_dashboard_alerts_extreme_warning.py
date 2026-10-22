"""Phase 11: `fulfillment_warning_minutes` has no product maximum (Spec SET-004: only `> 0`).
The Dashboard classification must therefore be overflow-safe for any positive INTEGER and
must keep the exact existing semantics (inclusive boundary) for ordinary values."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import dashboard_alerts as da

TZ = "America/New_York"
TODAY = date(2026, 6, 10)
PG_INTEGER_MAX = 2_147_483_647


def _utc(local: datetime) -> datetime:
    return local.replace(tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def _classify(*, now_local, warning, fdate=TODAY, ftime=time(15, 0), is_ready=False):
    return da.classify_fulfillment(
        is_ready=is_ready,
        fulfillment_date=fdate,
        fulfillment_time=ftime,
        now_utc=_utc(now_local),
        business_today=TODAY,
        warning_minutes=warning,
        business_timezone=TZ,
    )


def test_old_subtraction_form_really_overflows_for_the_extreme_value():
    """Documents the defect this change fixes: the previous formulation subtracted the
    window from the deadline and raises for a valid INTEGER-sized window."""
    deadline = da.fulfillment_deadline_utc(TODAY, time(15, 0), TZ)
    assert deadline is not None
    with pytest.raises(OverflowError):
        _ = deadline - timedelta(minutes=PG_INTEGER_MAX)


@pytest.mark.parametrize("warning", [PG_INTEGER_MAX, PG_INTEGER_MAX - 1, 1_000_000_000, 2_000_000])
def test_extreme_valid_warning_minutes_never_raise_and_flag_a_pending_order_at_risk(warning):
    # A not-Ready order whose deadline is later today is inside any enormous window.
    result = _classify(now_local=datetime(2026, 6, 10, 9, 0), warning=warning)
    assert result is da.FulfillmentTiming.AT_RISK


def test_extreme_warning_does_not_change_overdue_or_ready_precedence():
    assert (
        _classify(now_local=datetime(2026, 6, 10, 9, 0), warning=PG_INTEGER_MAX, is_ready=True)
        is da.FulfillmentTiming.ON_TRACK
    )
    assert (
        _classify(
            now_local=datetime(2026, 6, 10, 9, 0),
            warning=PG_INTEGER_MAX,
            fdate=date(2026, 6, 9),
        )
        is da.FulfillmentTiming.OVERDUE
    )


def test_extreme_warning_with_a_far_future_deadline_does_not_overflow():
    # Latest representable business date: the deadline sits near datetime.max.
    far = date(9999, 12, 30)
    result = da.classify_fulfillment(
        is_ready=False,
        fulfillment_date=far,
        fulfillment_time=time(23, 0),
        now_utc=_utc(datetime(2026, 6, 10, 9, 0)),
        business_today=TODAY,
        warning_minutes=PG_INTEGER_MAX,
        business_timezone="UTC",
    )
    assert result in {da.FulfillmentTiming.AT_RISK, da.FulfillmentTiming.ON_TRACK}


@pytest.mark.parametrize(
    ("now_local", "expected"),
    [
        (datetime(2026, 6, 10, 13, 59), da.FulfillmentTiming.ON_TRACK),
        (datetime(2026, 6, 10, 14, 0), da.FulfillmentTiming.AT_RISK),  # inclusive boundary
        (datetime(2026, 6, 10, 14, 1), da.FulfillmentTiming.AT_RISK),
        (datetime(2026, 6, 10, 15, 0), da.FulfillmentTiming.AT_RISK),
        (datetime(2026, 6, 10, 16, 30), da.FulfillmentTiming.AT_RISK),  # passed earlier today
    ],
)
def test_ordinary_window_boundary_is_unchanged(now_local, expected):
    assert _classify(now_local=now_local, warning=60) is expected


def test_minimum_window_of_one_minute_boundary_is_exact():
    assert (
        _classify(now_local=datetime(2026, 6, 10, 14, 58), warning=1)
        is da.FulfillmentTiming.ON_TRACK
    )
    assert (
        _classify(now_local=datetime(2026, 6, 10, 14, 59), warning=1)
        is da.FulfillmentTiming.AT_RISK
    )


def test_difference_form_is_equivalent_to_the_original_for_ordinary_values():
    """Exhaustive minute-by-minute equivalence with the original subtraction form across
    a window that straddles the boundary."""
    deadline = da.fulfillment_deadline_utc(TODAY, time(15, 0), TZ)
    assert deadline is not None
    for warning in (1, 15, 60, 90, 1440):
        for offset in range(-3000, 200, 7):
            now = deadline + timedelta(minutes=offset)
            original = now >= deadline - timedelta(minutes=warning)
            result = da.classify_fulfillment(
                is_ready=False,
                fulfillment_date=TODAY,
                fulfillment_time=time(15, 0),
                now_utc=now,
                business_today=TODAY,
                warning_minutes=warning,
                business_timezone=TZ,
            )
            assert (result is da.FulfillmentTiming.AT_RISK) == original
