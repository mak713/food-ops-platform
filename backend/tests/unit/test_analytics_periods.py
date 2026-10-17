"""Pure tests for the Analytics period resolver (Phase 10; Spec §13.2, §19.13)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.analytics_periods import (
    MAX_CUSTOM_RANGE_DAYS,
    AnalyticsRange,
    Granularity,
    build_buckets,
    granularity_for,
    local_day_start_utc,
    previous_period,
    resolve_periods,
)

CHICAGO = ZoneInfo("America/Chicago")
TODAY = date(2026, 10, 16)


def utc(y, m, d, h=0, mi=0, s=0, us=0):
    return datetime(y, m, d, h, mi, s, us, tzinfo=UTC)


def resolve(range_, *, start=None, end=None, today=TODAY, tz=CHICAGO, compare=True):
    periods, issues = resolve_periods(
        range_=range_,
        start_date=start,
        end_date=end,
        business_today=today,
        tz=tz,
        compare=compare,
    )
    assert issues == [], issues
    assert periods is not None
    return periods


# --------------------------------------------------------------------------------------
# Date Semantics Matrix (Chicago, today = Fri 2026-10-16)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("range_", "cur_start", "cur_end", "cur_lo", "cur_hi", "prev_start", "prev_end", "prev_lo"),
    [
        (
            AnalyticsRange.LAST_7_DAYS,
            date(2026, 10, 10),
            date(2026, 10, 16),
            utc(2026, 10, 10, 5),
            utc(2026, 10, 17, 5),
            date(2026, 10, 3),
            date(2026, 10, 9),
            utc(2026, 10, 3, 5),
        ),
        (
            AnalyticsRange.LAST_30_DAYS,
            date(2026, 9, 17),
            date(2026, 10, 16),
            utc(2026, 9, 17, 5),
            utc(2026, 10, 17, 5),
            date(2026, 8, 18),
            date(2026, 9, 16),
            utc(2026, 8, 18, 5),
        ),
        (
            AnalyticsRange.LAST_90_DAYS,
            date(2026, 7, 19),
            date(2026, 10, 16),
            utc(2026, 7, 19, 5),
            utc(2026, 10, 17, 5),
            date(2026, 4, 20),
            date(2026, 7, 18),
            utc(2026, 4, 20, 5),
        ),
        (
            AnalyticsRange.THIS_MONTH,
            date(2026, 10, 1),
            date(2026, 10, 16),
            utc(2026, 10, 1, 5),
            utc(2026, 10, 17, 5),
            date(2026, 9, 15),
            date(2026, 9, 30),
            utc(2026, 9, 15, 5),
        ),
        (
            AnalyticsRange.THIS_YEAR,
            date(2026, 1, 1),
            date(2026, 10, 16),
            utc(2026, 1, 1, 6),
            utc(2026, 10, 17, 5),
            date(2025, 3, 18),
            date(2025, 12, 31),
            utc(2025, 3, 18, 5),
        ),
    ],
)
def test_preset_matrix(range_, cur_start, cur_end, cur_lo, cur_hi, prev_start, prev_end, prev_lo):
    periods = resolve(range_)
    assert periods.current.start_date == cur_start
    assert periods.current.end_date == cur_end
    assert periods.current.lower_bound == cur_lo
    assert periods.current.upper_bound == cur_hi
    assert periods.previous is not None
    assert periods.previous.start_date == prev_start
    assert periods.previous.end_date == prev_end
    assert periods.previous.lower_bound == prev_lo
    # Immediately preceding: shared boundary, equal calendar-day count.
    assert periods.previous.upper_bound == periods.current.lower_bound
    assert periods.previous.days == periods.current.days


def test_last_n_days_are_calendar_days_including_today_not_rolling_hours():
    periods = resolve(AnalyticsRange.LAST_7_DAYS)
    assert periods.current.days == 7
    # Day boundaries, not "now - 7*24h": the upper bound is next local midnight.
    assert periods.current.upper_bound == utc(2026, 10, 17, 5)


def test_this_year_comparison_is_the_289_preceding_days_not_prior_year_to_date():
    periods = resolve(AnalyticsRange.THIS_YEAR)
    assert periods.current.days == 289
    assert periods.previous is not None
    assert periods.previous.days == 289
    assert periods.previous.end_date == date(2025, 12, 31)


def test_boundary_instants_membership():
    periods = resolve(AnalyticsRange.LAST_7_DAYS)
    cur = periods.current
    inside = utc(2026, 10, 17, 4, 59, 59, 999999)  # Oct 16 23:59:59.999999 CDT
    outside = utc(2026, 10, 17, 5)  # Oct 17 00:00:00 CDT
    assert cur.lower_bound <= inside < cur.upper_bound
    assert not (cur.lower_bound <= outside < cur.upper_bound)
    assert cur.lower_bound <= utc(2026, 10, 10, 5) < cur.upper_bound
    assert not (cur.lower_bound <= utc(2026, 10, 10, 4, 59, 59) < cur.upper_bound)


def test_custom_range_across_fall_back_dst():
    periods = resolve(AnalyticsRange.CUSTOM, start=date(2026, 10, 31), end=date(2026, 11, 2))
    assert periods.current.lower_bound == utc(2026, 10, 31, 5)
    assert periods.current.upper_bound == utc(2026, 11, 3, 6)
    assert periods.current.days == 3
    # Nov 1 2026 is a 25-hour local day.
    nov1 = resolve(AnalyticsRange.CUSTOM, start=date(2026, 11, 1), end=date(2026, 11, 1))
    assert nov1.current.upper_bound - nov1.current.lower_bound == timedelta(hours=25)
    assert periods.previous is not None
    assert periods.previous.lower_bound == utc(2026, 10, 28, 5)
    assert periods.previous.upper_bound == utc(2026, 10, 31, 5)


def test_custom_range_spring_forward_day_is_23_hours():
    day = resolve(AnalyticsRange.CUSTOM, start=date(2026, 3, 8), end=date(2026, 3, 8))
    assert day.current.lower_bound == utc(2026, 3, 8, 6)
    assert day.current.upper_bound == utc(2026, 3, 9, 5)
    assert day.current.upper_bound - day.current.lower_bound == timedelta(hours=23)
    assert day.previous is not None
    assert day.previous.lower_bound == utc(2026, 3, 7, 6)
    assert day.previous.upper_bound == utc(2026, 3, 8, 6)


@pytest.mark.parametrize(
    ("tz_name", "day", "expected_lo"),
    [
        ("Asia/Kolkata", date(2026, 10, 16), utc(2026, 10, 15, 18, 30)),
        ("Pacific/Auckland", date(2026, 10, 16), utc(2026, 10, 15, 11)),
        ("UTC", date(2026, 10, 16), utc(2026, 10, 16)),
    ],
)
def test_non_chicago_offsets(tz_name, day, expected_lo):
    assert local_day_start_utc(day, ZoneInfo(tz_name)) == expected_lo


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def test_custom_requires_both_dates():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=None,
        end_date=None,
        business_today=TODAY,
        tz=CHICAGO,
        compare=False,
    )
    assert periods is None
    assert {i.code for i in issues} == {
        "ANALYTICS_CUSTOM_START_REQUIRED",
        "ANALYTICS_CUSTOM_END_REQUIRED",
    }


def test_custom_start_after_end_rejected():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(2026, 10, 5),
        end_date=date(2026, 10, 4),
        business_today=TODAY,
        tz=CHICAGO,
        compare=False,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_RANGE_START_AFTER_END"]


def test_single_day_custom_range_is_valid():
    periods = resolve(AnalyticsRange.CUSTOM, start=TODAY, end=TODAY)
    assert periods.current.days == 1


@pytest.mark.parametrize("range_", [r for r in AnalyticsRange if r is not AnalyticsRange.CUSTOM])
def test_presets_reject_supplied_custom_dates(range_):
    periods, issues = resolve_periods(
        range_=range_,
        start_date=date(2026, 1, 1),
        end_date=None,
        business_today=TODAY,
        tz=CHICAGO,
        compare=False,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_PRESET_DATES_NOT_ALLOWED"]


def test_custom_range_of_exactly_3660_days_is_accepted():
    end = date(2026, 10, 16)
    start = end - timedelta(days=3659)
    periods = resolve(AnalyticsRange.CUSTOM, start=start, end=end, compare=False)
    assert periods.current.days == MAX_CUSTOM_RANGE_DAYS == 3660
    # Granularity for such a span is monthly, so the bucket skeleton stays small.
    granularity, buckets = build_buckets(periods.current, CHICAGO)
    assert granularity is Granularity.MONTH
    assert len(buckets) == (end.year * 12 + end.month) - (start.year * 12 + start.month) + 1


def test_custom_range_of_3661_days_is_rejected_with_details_not_truncated():
    end = date(2026, 10, 16)
    start = end - timedelta(days=3660)
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=start,
        end_date=end,
        business_today=TODAY,
        tz=CHICAGO,
        compare=False,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_RANGE_TOO_LARGE"]
    assert issues[0].details == {"requested_days": 3661, "maximum_days": 3660}
    assert "3,660" in issues[0].message and "3,661" in issues[0].message


def test_extreme_range_is_rejected_by_size_before_any_bucket_work():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(1, 1, 5),
        end_date=date(9999, 12, 30),
        business_today=TODAY,
        tz=ZoneInfo("UTC"),
        compare=True,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_RANGE_TOO_LARGE"]


def test_reversed_range_reports_order_not_size():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(2026, 10, 16),
        end_date=date(1990, 1, 1),
        business_today=TODAY,
        tz=CHICAGO,
        compare=False,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_RANGE_START_AFTER_END"]


@pytest.mark.parametrize("range_", [r for r in AnalyticsRange if r is not AnalyticsRange.CUSTOM])
def test_presets_are_unaffected_by_the_custom_range_limit(range_):
    periods = resolve(range_)
    assert periods.current.days <= 366 < MAX_CUSTOM_RANGE_DAYS


def test_previous_period_underflow_is_structured_not_clamped():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(1, 1, 5),
        end_date=date(1, 1, 20),
        business_today=TODAY,
        tz=ZoneInfo("UTC"),
        compare=True,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_PREVIOUS_PERIOD_NOT_REPRESENTABLE"]
    assert issues[0].field == "compare"


def test_same_extreme_range_without_compare_is_resolved_if_representable():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(1, 1, 5),
        end_date=date(1, 1, 20),
        business_today=TODAY,
        tz=ZoneInfo("UTC"),
        compare=False,
    )
    assert issues == []
    assert periods is not None and periods.previous is None


def test_date_max_end_is_not_representable():
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(9999, 12, 30),
        end_date=date(9999, 12, 31),
        business_today=TODAY,
        tz=ZoneInfo("UTC"),
        compare=False,
    )
    assert periods is None
    assert [i.code for i in issues] == ["ANALYTICS_RANGE_NOT_REPRESENTABLE"]


# --------------------------------------------------------------------------------------
# Midnight-offset-transition regression coverage (partition + first-occurrence rules)
# --------------------------------------------------------------------------------------


def _assert_partition(tz_name: str, first_day: date, n_days: int) -> None:
    """Adjacent days share exactly one UTC boundary; every sampled UTC instant belongs to
    exactly one day, and that day equals the instant's Business-local civil date."""
    tz = ZoneInfo(tz_name)
    days = [first_day + timedelta(days=i) for i in range(n_days)]
    intervals = []
    for d in days:
        p = resolve(AnalyticsRange.CUSTOM, start=d, end=d, tz=tz, compare=False).current
        assert p.lower_bound <= p.upper_bound
        intervals.append((d, p.lower_bound, p.upper_bound))
    for (_, _, hi), (_, lo2, _) in zip(intervals, intervals[1:], strict=False):
        assert hi == lo2  # no gap, no overlap

    t = intervals[0][1]
    end = intervals[-1][2]
    step = timedelta(minutes=15)
    while t < end:
        owners = [d for d, lo, hi in intervals if lo <= t < hi]
        assert len(owners) == 1, (tz_name, t, owners)
        assert owners[0] == t.astimezone(tz).date(), (tz_name, t, owners)
        t += step


@pytest.mark.parametrize(
    ("tz_name", "first_day", "n_days"),
    [
        ("America/Chicago", date(2026, 3, 6), 5),  # spring-forward 02:00
        ("America/Chicago", date(2026, 10, 30), 5),  # fall-back 02:00
        ("America/Sao_Paulo", date(2018, 11, 2), 5),  # nonexistent midnight (2018-11-04)
        ("America/Havana", date(2020, 3, 6), 5),  # nonexistent midnight (spring-forward)
        ("America/Havana", date(2020, 11, 1), 4),  # ambiguous midnight (fall-back)
        ("Pacific/Apia", date(2011, 12, 28), 5),  # skipped civil date 2011-12-30
        ("Pacific/Kiritimati", date(1994, 12, 29), 5),  # skipped civil date 1994-12-31
        ("Asia/Beirut", date(2023, 3, 24), 5),  # midnight transition
        ("Africa/Cairo", date(2023, 4, 26), 5),  # midnight transition (DST returned 2023)
        ("Asia/Kolkata", date(2026, 10, 14), 4),
        ("Pacific/Auckland", date(2026, 9, 25), 5),
    ],
)
def test_adjacent_days_partition_utc_time_exactly(tz_name, first_day, n_days):
    _assert_partition(tz_name, first_day, n_days)


def test_nonexistent_midnight_resolves_to_transition_instant():
    tz = ZoneInfo("America/Sao_Paulo")
    # 2018-11-04: clocks jumped 00:00 -> 01:00 (UTC-3 -> UTC-2); transition at 03:00Z.
    assert local_day_start_utc(date(2018, 11, 4), tz) == utc(2018, 11, 4, 3)
    # The previous day therefore ends exactly there (and is 24h long).
    assert local_day_start_utc(date(2018, 11, 3), tz) == utc(2018, 11, 3, 3)


def test_ambiguous_midnight_chooses_first_occurrence():
    tz = ZoneInfo("America/Havana")
    # 2020-11-01: clocks fell back 01:00 -> 00:00 (UTC-4 -> UTC-5); 00:00 occurs twice.
    # First occurrence = still UTC-4 => 04:00Z (the second would be 05:00Z).
    assert local_day_start_utc(date(2020, 11, 1), tz) == utc(2020, 11, 1, 4)
    # An instant in the repeated hour belongs to 2020-11-01, not 10-31.
    during_second = utc(2020, 11, 1, 5, 30)
    p = resolve(
        AnalyticsRange.CUSTOM, start=date(2020, 11, 1), end=date(2020, 11, 1), tz=tz, compare=False
    ).current
    assert p.lower_bound <= during_second < p.upper_bound


def test_skipped_civil_date_is_an_empty_interval_with_no_gap():
    tz = ZoneInfo("Pacific/Apia")
    skipped = resolve(
        AnalyticsRange.CUSTOM,
        start=date(2011, 12, 30),
        end=date(2011, 12, 30),
        tz=tz,
        compare=False,
    ).current
    assert skipped.lower_bound == skipped.upper_bound
    before = resolve(
        AnalyticsRange.CUSTOM,
        start=date(2011, 12, 29),
        end=date(2011, 12, 29),
        tz=tz,
        compare=False,
    ).current
    after = resolve(
        AnalyticsRange.CUSTOM,
        start=date(2011, 12, 31),
        end=date(2011, 12, 31),
        tz=tz,
        compare=False,
    ).current
    assert before.upper_bound == skipped.lower_bound == after.lower_bound


def test_multi_day_period_over_midnight_transition_is_contiguous_with_neighbour():
    tz = ZoneInfo("America/Sao_Paulo")
    periods = resolve(
        AnalyticsRange.CUSTOM, start=date(2018, 11, 4), end=date(2018, 11, 6), tz=tz, compare=True
    )
    assert periods.previous is not None
    assert periods.previous.upper_bound == periods.current.lower_bound == utc(2018, 11, 4, 3)
    assert previous_period(periods.current, tz) == periods.previous


# --------------------------------------------------------------------------------------
# Granularity and buckets
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, Granularity.DAY),
        (31, Granularity.DAY),
        (32, Granularity.WEEK),
        (183, Granularity.WEEK),
        (184, Granularity.MONTH),
        (365, Granularity.MONTH),
    ],
)
def test_granularity_thresholds(days, expected):
    start = date(2026, 1, 1)
    p = resolve(
        AnalyticsRange.CUSTOM, start=start, end=start + timedelta(days=days - 1), compare=False
    ).current
    assert granularity_for(p) is expected


def test_preset_granularities():
    assert granularity_for(resolve(AnalyticsRange.LAST_7_DAYS).current) is Granularity.DAY
    assert granularity_for(resolve(AnalyticsRange.LAST_30_DAYS).current) is Granularity.DAY
    assert granularity_for(resolve(AnalyticsRange.THIS_MONTH).current) is Granularity.DAY
    assert granularity_for(resolve(AnalyticsRange.LAST_90_DAYS).current) is Granularity.WEEK
    assert granularity_for(resolve(AnalyticsRange.THIS_YEAR).current) is Granularity.MONTH


def test_daily_buckets_zero_fill_every_day():
    period = resolve(AnalyticsRange.LAST_7_DAYS).current
    granularity, buckets = build_buckets(period, CHICAGO)
    assert granularity is Granularity.DAY
    assert [b.label for b in buckets] == [f"2026-10-{d:02d}" for d in range(10, 17)]
    assert buckets[0].lower_bound == period.lower_bound
    assert buckets[-1].upper_bound == period.upper_bound
    for a, b in zip(buckets, buckets[1:], strict=False):
        assert a.upper_bound == b.lower_bound


def test_weekly_buckets_start_monday_and_are_clipped():
    period = resolve(AnalyticsRange.LAST_90_DAYS).current  # Sun Jul 19 .. Fri Oct 16
    granularity, buckets = build_buckets(period, CHICAGO)
    assert granularity is Granularity.WEEK
    assert buckets[0].start_date == date(2026, 7, 19)  # Sunday, clipped (1 day)
    assert buckets[0].end_date == date(2026, 7, 19)
    assert buckets[1].start_date == date(2026, 7, 20)  # Monday
    assert buckets[1].end_date == date(2026, 7, 26)  # Sunday
    assert buckets[-1].start_date == date(2026, 10, 12)  # Monday
    assert buckets[-1].end_date == date(2026, 10, 16)  # clipped to period end
    for b in buckets[1:-1]:
        assert b.start_date.weekday() == 0 and b.end_date.weekday() == 6
    assert buckets[0].lower_bound == period.lower_bound
    assert buckets[-1].upper_bound == period.upper_bound


def test_monthly_buckets_are_clipped_calendar_months():
    period = resolve(AnalyticsRange.THIS_YEAR).current
    granularity, buckets = build_buckets(period, CHICAGO)
    assert granularity is Granularity.MONTH
    assert [b.label for b in buckets] == [f"2026-{m:02d}" for m in range(1, 11)]
    assert buckets[0].start_date == date(2026, 1, 1)
    assert buckets[-1].start_date == date(2026, 10, 1)
    assert buckets[-1].end_date == date(2026, 10, 16)
    # Monthly bounds still use Business-local midnights (CST in Jan, CDT in Apr).
    assert buckets[0].lower_bound == utc(2026, 1, 1, 6)
    assert buckets[3].lower_bound == utc(2026, 4, 1, 5)


def test_buckets_partition_the_period_across_dst():
    period = resolve(
        AnalyticsRange.CUSTOM, start=date(2026, 1, 1), end=date(2026, 12, 31), compare=False
    ).current
    _, buckets = build_buckets(period, CHICAGO)
    assert buckets[0].lower_bound == period.lower_bound
    assert buckets[-1].upper_bound == period.upper_bound
    for a, b in zip(buckets, buckets[1:], strict=False):
        assert a.upper_bound == b.lower_bound
        assert a.end_date + timedelta(days=1) == b.start_date


@pytest.mark.parametrize("tz_name", ["UTC", "America/Chicago", "Pacific/Auckland"])
def test_buckets_do_not_overflow_at_the_end_of_the_supported_date_range(tz_name):
    tz = ZoneInfo(tz_name)
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(9999, 6, 1),
        end_date=date(9999, 12, 30),
        business_today=TODAY,
        tz=tz,
        compare=False,
    )
    assert not issues and periods is not None
    granularity, buckets = build_buckets(periods.current, tz)
    assert granularity is Granularity.MONTH
    assert buckets[-1].end_date == date(9999, 12, 30)
    assert buckets[-1].upper_bound == periods.current.upper_bound
    weekly, week_buckets = build_buckets(
        resolve(
            AnalyticsRange.CUSTOM,
            start=date(9999, 11, 1),
            end=date(9999, 12, 30),
            tz=tz,
            compare=False,
        ).current,
        tz,
    )
    assert weekly is Granularity.WEEK
    assert week_buckets[-1].end_date == date(9999, 12, 30)


def test_buckets_do_not_underflow_at_the_start_of_the_supported_date_range():
    tz = ZoneInfo("UTC")
    period = resolve(
        AnalyticsRange.CUSTOM, start=date(1, 1, 1), end=date(1, 3, 1), tz=tz, compare=False
    ).current
    _, buckets = build_buckets(period, tz)
    assert buckets[0].start_date == date(1, 1, 1)
    assert buckets[0].lower_bound == period.lower_bound
