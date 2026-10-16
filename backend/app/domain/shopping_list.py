"""Deterministic Shopping List calculators (Spec §6.10 PLAN-011, §12.12, §14.9; ADR-130;
Phase 9 approved range rules).

Pure, side-effect-free: no HTTP or database access and no `ApiError` import (ADR-109,
including its Phase 7 addendum — representability/validation problems are reported as
plain values, never raised as application exceptions; translating them into a structured
`422` is the service layer's job). Every Decimal operation runs inside its own explicit,
fixed-precision `decimal.localcontext()` so a result can never depend on the caller's
ambient Decimal context.

Cumulative-horizon shortage (ADR-130)::

    cumulative_reserved_through(D) = SUM(reservation.quantity WHERE demand_date <= D)
    shortage_through(D)            = max(0, cumulative_reserved_through(D) - physical_quantity)

`physical_quantity` is subtracted exactly once, at each date threshold — never as a
separate "available pool" that would double-count it. Overdue reservations
(`demand_date` before the first display date) are always `<= D`, so they participate at
every in-range `D`; a reservation dated after `D` never contributes to `D`.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

_CONTEXT_PRECISION = 50
"""Matches the isolation precision every deterministic calculator uses (ADR-109)."""

ALLOWED_PRESET_HORIZON_DAYS: tuple[int, ...] = (3, 7)
"""Spec §8.4: `shopping_horizon_days IN (3, 7)`. The preset API accepts only these."""

MAX_CUSTOM_RANGE_DAYS = 90
"""Approved Phase 9 product rule: a custom range spans at most 90 calendar days,
counted inclusively (`end - start + 1 <= 90`)."""


@dataclass(frozen=True)
class DateShortage:
    demand_date: date
    cumulative_reserved_through_date: Decimal
    cumulative_shortage_through_date: Decimal


@dataclass(frozen=True)
class RangeIssue:
    code: str
    message: str
    field: str | None


@dataclass(frozen=True)
class ResolvedShoppingRange:
    start_date: date
    end_date: date
    horizon_days: int | None
    """The preset (3 or 7) when the range came from a preset/default; `None` for a custom
    range."""
    is_custom: bool

    def display_dates(self) -> list[date]:
        span = (self.end_date - self.start_date).days
        return [self.start_date + timedelta(days=offset) for offset in range(span + 1)]


def resolve_shopping_range(
    *,
    business_today: date,
    default_horizon_days: int,
    horizon_days: int | None,
    start_date: date | None,
    end_date: date | None,
) -> tuple[ResolvedShoppingRange | None, list[RangeIssue]]:
    """Resolves the request's range parameters into one concrete, validated range, or a
    list of issues (never raising).

    - No parameters -> the Business default preset.
    - `horizon_days` alone -> a preset (must be 3 or 7) covering `[today, today + N]`
      (ADR-130's established `N + 1` display dates).
    - `start_date` + `end_date` -> a custom range: `start >= business_today`,
      `end >= start`, and at most `MAX_CUSTOM_RANGE_DAYS` calendar days inclusive.
    - Any mixture (a preset together with custom bounds, or only one custom bound) is
      rejected rather than silently resolved by picking one interpretation.
    """
    has_custom_bound = start_date is not None or end_date is not None

    if horizon_days is not None and has_custom_bound:
        return None, [
            RangeIssue(
                "SHOPPING_LIST_RANGE_CONFLICT",
                "Provide either a preset horizon or a custom start/end range, not both.",
                "horizon_days",
            )
        ]

    if has_custom_bound:
        if start_date is None or end_date is None:
            missing = "end_date" if start_date is not None else "start_date"
            return None, [
                RangeIssue(
                    "SHOPPING_LIST_RANGE_INCOMPLETE",
                    "A custom range requires both a start date and an end date.",
                    missing,
                )
            ]
        issues: list[RangeIssue] = []
        if start_date < business_today:
            issues.append(
                RangeIssue(
                    "SHOPPING_LIST_RANGE_START_IN_PAST",
                    "The range start cannot be before today.",
                    "start_date",
                )
            )
        if end_date < start_date:
            issues.append(
                RangeIssue(
                    "SHOPPING_LIST_RANGE_END_BEFORE_START",
                    "The range end cannot be before its start.",
                    "end_date",
                )
            )
        elif (end_date - start_date).days + 1 > MAX_CUSTOM_RANGE_DAYS:
            issues.append(
                RangeIssue(
                    "SHOPPING_LIST_RANGE_TOO_LONG",
                    f"A custom range can span at most {MAX_CUSTOM_RANGE_DAYS} days.",
                    "end_date",
                )
            )
        if issues:
            return None, issues
        return ResolvedShoppingRange(start_date, end_date, None, True), []

    chosen = horizon_days if horizon_days is not None else default_horizon_days
    if chosen not in ALLOWED_PRESET_HORIZON_DAYS:
        return None, [
            RangeIssue(
                "SHOPPING_LIST_HORIZON_INVALID",
                "The horizon must be 3 or 7 days.",
                "horizon_days",
            )
        ]
    return (
        ResolvedShoppingRange(
            business_today, business_today + timedelta(days=chosen), chosen, False
        ),
        [],
    )


def cumulative_reserved_through(
    reservations: Iterable[tuple[date, Decimal]], through: date
) -> Decimal:
    """`SUM(quantity WHERE demand_date <= through)`."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return sum(
            (quantity for demand_date, quantity in reservations if demand_date <= through),
            Decimal(0),
        )


def shortage_through(
    reservations: Iterable[tuple[date, Decimal]], physical_quantity: Decimal, through: date
) -> Decimal:
    """`max(0, cumulative_reserved_through(through) - physical_quantity)`."""
    reserved = cumulative_reserved_through(reservations, through)
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return max(Decimal(0), reserved - physical_quantity)


def build_timeline(
    reservations: Sequence[tuple[date, Decimal]],
    physical_quantity: Decimal,
    display_dates: Iterable[date],
) -> list[DateShortage]:
    """One `DateShortage` per display date, in the order given."""
    ordered = sorted(reservations, key=lambda r: r[0])
    timeline: list[DateShortage] = []
    for display_date in display_dates:
        reserved = cumulative_reserved_through(ordered, display_date)
        with decimal.localcontext() as ctx:
            ctx.prec = _CONTEXT_PRECISION
            shortage = max(Decimal(0), reserved - physical_quantity)
        timeline.append(DateShortage(display_date, reserved, shortage))
    return timeline


def first_shortage_date(timeline: Iterable[DateShortage]) -> date | None:
    """The earliest display date at which a shortage exists ("needed by"), or `None`."""
    for entry in timeline:
        if entry.cumulative_shortage_through_date > 0:
            return entry.demand_date
    return None
