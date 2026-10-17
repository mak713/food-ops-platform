"""Authoritative Business-local Analytics reporting periods (Spec §13.2, ANA-002, ANA-010;
Phase 10 approved period contract).

Pure, side-effect-free: no HTTP or database access and no `ApiError` import (ADR-109).
Problems are reported as plain `PeriodIssue` values; translating them into the project's
structured `422 ANALYTICS_RANGE_INVALID` is the service/router's job.

The model
---------
A reporting period is a run of *N Business-local calendar days*, inclusive for the seller:
``[start_date, end_date]``. The SQL predicate is **half-open on UTC instants**::

    completed_at >= lower_bound  AND  completed_at < upper_bound

where ``lower_bound = local_day_start_utc(start_date)`` and
``upper_bound = local_day_start_utc(end_date + 1 day)``.

Every boundary — period edge, previous-period edge, and trend-bucket edge — is produced by
the *single* function :func:`local_day_start_utc`, so two adjacent calendar periods share
exactly the same UTC instant: no instant is counted twice and none falls through a gap,
including across DST transitions and zones whose transition happens at (or skips) local
midnight.

Local-midnight resolution rule (deterministic):

* ordinary day: the unique instant of ``00:00`` local;
* **ambiguous** midnight (clocks repeat ``00:xx``, e.g. ``America/Havana`` fall-back):
  the *first* occurrence (``fold=0``), so the day starts as early as it can;
* **nonexistent** midnight (clocks skip ``00:00``, e.g. ``America/Sao_Paulo`` 2018-11-04,
  ``America/Havana`` spring-forward): ``fold=0`` uses the pre-transition UTC offset, which
  yields exactly the transition instant — the first instant that actually exists on that
  civil date;
* **skipped civil date** (e.g. ``Pacific/Apia`` 2011-12-30): both neighbouring midnights
  resolve to the same instant, so the skipped date is an empty interval and its neighbours
  still partition time with no gap.

Last N Days = Business-local today and the N-1 preceding calendar days (today is partial);
it is *not* N x 24 rolling hours.

The previous equivalent period of a current period of N calendar days is the N calendar
days immediately preceding it::

    previous_end   = current_start - 1 day
    previous_start = current_start - N days
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

DAILY_MAX_DAYS = 31
"""Periods of at most this many calendar days are bucketed daily."""

MAX_CUSTOM_RANGE_DAYS = 3660
"""Technical resource guard (approved after measured evidence): a Custom Range spans at most
3,660 inclusive Business-local calendar days. It is enforced here — in the shared period
contract — so every Analytics report and CSV accepts or rejects exactly the same ranges, and a
rejected range is never clamped or truncated. Presets are unaffected (<= 366 days)."""

WEEKLY_MAX_DAYS = 183
"""Periods of 32..183 calendar days are bucketed weekly; longer periods monthly."""


class AnalyticsRange(enum.StrEnum):
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"
    LAST_90_DAYS = "LAST_90_DAYS"
    THIS_MONTH = "THIS_MONTH"
    THIS_YEAR = "THIS_YEAR"
    CUSTOM = "CUSTOM"


_PRESET_DAYS = {
    AnalyticsRange.LAST_7_DAYS: 7,
    AnalyticsRange.LAST_30_DAYS: 30,
    AnalyticsRange.LAST_90_DAYS: 90,
}


class Granularity(enum.StrEnum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@dataclass(frozen=True)
class PeriodIssue:
    code: str
    message: str
    field: str | None
    details: dict | None = None


@dataclass(frozen=True)
class Period:
    """``[start_date, end_date]`` inclusive Business-local calendar days, mapped to the UTC
    half-open interval ``[lower_bound, upper_bound)``."""

    start_date: date
    end_date: date
    lower_bound: datetime
    upper_bound: datetime

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


@dataclass(frozen=True)
class ResolvedPeriods:
    range: AnalyticsRange
    current: Period
    previous: Period | None


@dataclass(frozen=True)
class Bucket:
    index: int
    label: str
    start_date: date
    end_date: date
    lower_bound: datetime
    upper_bound: datetime


def local_day_start_utc(day: date, tz: tzinfo) -> datetime:
    """The UTC instant at which Business-local calendar ``day`` begins (see module
    docstring for the ambiguous / nonexistent / skipped-date rules). ``fold=0`` is what
    makes ambiguous midnights choose the first occurrence and nonexistent midnights
    resolve to the transition instant.

    Raises ``OverflowError``/``ValueError`` when the instant is not representable."""
    return datetime.combine(day, time.min, tzinfo=tz).replace(fold=0).astimezone(UTC)


def _build_period(start: date, end: date, tz: tzinfo) -> Period:
    """Raises ``OverflowError`` / ``ValueError`` for non-representable dates."""
    lower = local_day_start_utc(start, tz)
    upper = local_day_start_utc(end + timedelta(days=1), tz)
    return Period(start_date=start, end_date=end, lower_bound=lower, upper_bound=upper)


def previous_period(current: Period, tz: tzinfo) -> Period:
    """The N calendar days immediately preceding ``current`` (shares its lower bound as
    the previous period's upper bound). Raises ``OverflowError`` / ``ValueError`` when the
    preceding period cannot be represented."""
    n = current.days
    prev_end = current.start_date - timedelta(days=1)
    prev_start = current.start_date - timedelta(days=n)
    return _build_period(prev_start, prev_end, tz)


def resolve_periods(
    *,
    range_: AnalyticsRange,
    start_date: date | None,
    end_date: date | None,
    business_today: date,
    tz: tzinfo,
    compare: bool,
) -> tuple[ResolvedPeriods | None, list[PeriodIssue]]:
    """Resolves the selected period (and, when ``compare`` is set, the immediately
    preceding equivalent-duration period). Returns ``(None, issues)`` on any violation;
    never clamps or truncates."""
    issues: list[PeriodIssue] = []

    if range_ is AnalyticsRange.CUSTOM:
        if start_date is None:
            issues.append(
                PeriodIssue(
                    "ANALYTICS_CUSTOM_START_REQUIRED",
                    "start_date is required for a Custom Range.",
                    "start_date",
                )
            )
        if end_date is None:
            issues.append(
                PeriodIssue(
                    "ANALYTICS_CUSTOM_END_REQUIRED",
                    "end_date is required for a Custom Range.",
                    "end_date",
                )
            )
        if issues:
            return None, issues
        assert start_date is not None and end_date is not None
        if start_date > end_date:
            return None, [
                PeriodIssue(
                    "ANALYTICS_RANGE_START_AFTER_END",
                    "start_date must be on or before end_date.",
                    "start_date",
                )
            ]
        requested_days = (end_date - start_date).days + 1
        if requested_days > MAX_CUSTOM_RANGE_DAYS:
            return None, [
                PeriodIssue(
                    "ANALYTICS_RANGE_TOO_LARGE",
                    f"A Custom Range can span at most {MAX_CUSTOM_RANGE_DAYS:,} days "
                    f"(requested {requested_days:,}).",
                    "end_date",
                    {
                        "requested_days": requested_days,
                        "maximum_days": MAX_CUSTOM_RANGE_DAYS,
                    },
                )
            ]
        start, end = start_date, end_date
    else:
        if start_date is not None or end_date is not None:
            return None, [
                PeriodIssue(
                    "ANALYTICS_PRESET_DATES_NOT_ALLOWED",
                    "start_date and end_date are only valid with range=CUSTOM.",
                    "start_date" if start_date is not None else "end_date",
                )
            ]
        end = business_today
        if range_ in _PRESET_DAYS:
            start = business_today - timedelta(days=_PRESET_DAYS[range_] - 1)
        elif range_ is AnalyticsRange.THIS_MONTH:
            start = business_today.replace(day=1)
        else:  # THIS_YEAR
            start = business_today.replace(month=1, day=1)

    try:
        current = _build_period(start, end, tz)
    except OverflowError, ValueError:
        return None, [
            PeriodIssue(
                "ANALYTICS_RANGE_NOT_REPRESENTABLE",
                "The selected range cannot be represented.",
                "start_date" if range_ is AnalyticsRange.CUSTOM else "range",
            )
        ]

    previous: Period | None = None
    if compare:
        try:
            previous = previous_period(current, tz)
        except OverflowError, ValueError:
            return None, [
                PeriodIssue(
                    "ANALYTICS_PREVIOUS_PERIOD_NOT_REPRESENTABLE",
                    "The immediately preceding equivalent-duration period cannot be "
                    "represented for the selected range.",
                    "compare",
                )
            ]

    return ResolvedPeriods(range=range_, current=current, previous=previous), []


def granularity_for(period: Period) -> Granularity:
    days = period.days
    if days <= DAILY_MAX_DAYS:
        return Granularity.DAY
    if days <= WEEKLY_MAX_DAYS:
        return Granularity.WEEK
    return Granularity.MONTH


def _month_end(day: date, limit: date) -> date:
    """Last day of `day`'s calendar month, never beyond `limit` (the period's own end, so the
    arithmetic cannot overflow at the end of the supported date range)."""
    if day.month == 12:
        first_of_next = date(day.year + 1, 1, 1) if day.year < date.max.year else None
    else:
        first_of_next = date(day.year, day.month + 1, 1)
    if first_of_next is None:
        return limit
    return min(first_of_next - timedelta(days=1), limit)


def build_buckets(period: Period, tz: tzinfo) -> tuple[Granularity, list[Bucket]]:
    """Zero-fill skeleton: every bucket of the period, in order, clipped to the period.

    Weekly buckets start on **Monday** (ISO convention) and are clipped at both ends;
    monthly buckets are calendar months, clipped. Bucket bounds come from
    :func:`local_day_start_utc`, so ``sum(buckets) == period`` exactly: bucket ``i``'s
    upper bound is bucket ``i+1``'s lower bound and the first/last bounds equal the
    period's own."""
    granularity = granularity_for(period)
    spans: list[tuple[date, date]] = []  # inclusive start, inclusive end
    cursor = period.start_date
    while cursor <= period.end_date:
        if granularity is Granularity.DAY:
            seg_end = cursor
        elif granularity is Granularity.WEEK:
            # Monday=0 ... Sunday=6; clipped to the period end without overflowing at date.max.
            days_to_sunday = min(6 - cursor.weekday(), (period.end_date - cursor).days)
            seg_end = cursor + timedelta(days=days_to_sunday)
        else:
            seg_end = _month_end(cursor, period.end_date)
        spans.append((cursor, seg_end))
        cursor = seg_end + timedelta(days=1)

    boundaries = [period.lower_bound]
    for _, seg_end in spans[:-1]:
        boundaries.append(local_day_start_utc(seg_end + timedelta(days=1), tz))
    boundaries.append(period.upper_bound)

    buckets: list[Bucket] = []
    for i, (seg_start, seg_end) in enumerate(spans):
        if granularity is Granularity.MONTH:
            label = f"{seg_start.year:04d}-{seg_start.month:02d}"
        else:
            label = seg_start.isoformat()
        buckets.append(
            Bucket(
                index=i,
                label=label,
                start_date=seg_start,
                end_date=seg_end,
                lower_bound=boundaries[i],
                upper_bound=boundaries[i + 1],
            )
        )
    return granularity, buckets
