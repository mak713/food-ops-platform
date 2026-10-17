"""Deterministic Analytics KPI calculators (Spec §13.3, ANA-005..ANA-008; Phase 10).

Pure, side-effect-free (ADR-109): no HTTP or database access; every Decimal operation runs
inside its own fixed-precision `decimal.localcontext()` so results never depend on the
caller's ambient context. The database performs all *summation*; this module only derives
ratios/differences from already-summed values and quantizes them at one point::

    Contribution        = Revenue - Direct Cost                (cents, may be negative)
    Contribution Margin = Contribution / Revenue               (6 dp RATIO, None if Revenue = 0)
    AOV                 = Revenue / Completed Orders           (cents, None if Orders = 0)

Quantization: money to cents and ratios to 6 dp, both ROUND_HALF_UP (ADR-115 convention).
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal

_CONTEXT_PRECISION = 50
_MONEY_QUANTUM = Decimal("0.01")
_RATIO_QUANTUM = Decimal("0.000001")
_ROUNDING = decimal.ROUND_HALF_UP


def quantize_money(value: Decimal) -> Decimal:
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_MONEY_QUANTUM, rounding=_ROUNDING)


def quantize_ratio(value: Decimal) -> Decimal:
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_RATIO_QUANTUM, rounding=_ROUNDING)


def contribution(revenue: Decimal, direct_cost: Decimal) -> Decimal:
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return quantize_money(revenue - direct_cost)


def contribution_margin(contribution_value: Decimal, revenue: Decimal) -> Decimal | None:
    """`None` when Revenue is zero (safe state, never a division by zero)."""
    if revenue == 0:
        return None
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return quantize_ratio(contribution_value / revenue)


def average_order_value(revenue: Decimal, completed_orders: int) -> Decimal | None:
    """`None` when there are no Completed Orders."""
    if completed_orders == 0:
        return None
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return quantize_money(revenue / Decimal(completed_orders))


@dataclass(frozen=True)
class Kpis:
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None
    completed_orders: int
    average_order_value: Decimal | None


def compute_kpis(*, revenue: Decimal, direct_cost: Decimal, completed_orders: int) -> Kpis:
    revenue_q = quantize_money(revenue)
    cost_q = quantize_money(direct_cost)
    contribution_value = contribution(revenue_q, cost_q)
    return Kpis(
        revenue=revenue_q,
        direct_cost=cost_q,
        contribution=contribution_value,
        contribution_margin=contribution_margin(contribution_value, revenue_q),
        completed_orders=completed_orders,
        average_order_value=average_order_value(revenue_q, completed_orders),
    )


@dataclass(frozen=True)
class MoneyDelta:
    """`absolute` is current - previous (cents); `percent_change` is the 6 dp ratio
    (0.125 = +12.5%) or `None` when the previous value is missing or not positive — a
    percentage change relative to zero (or to a loss) is undefined, never fabricated."""

    absolute: Decimal | None
    percent_change: Decimal | None


@dataclass(frozen=True)
class CountDelta:
    """Integer count delta kept semantically typed as a count, not a monetary Decimal."""

    absolute: int
    percent_change: Decimal | None


@dataclass(frozen=True)
class RateDelta:
    """Contribution Margin comparison: a percentage-point (rate) difference, 6 dp ratio
    units (0.05 = +5 points). Never a second percentage-growth calculation."""

    absolute: Decimal | None


def money_delta(current: Decimal | None, previous: Decimal | None) -> MoneyDelta:
    if current is None or previous is None:
        return MoneyDelta(absolute=None, percent_change=None)
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        absolute = quantize_money(current - previous)
        percent = quantize_ratio((current - previous) / previous) if previous > 0 else None
    return MoneyDelta(absolute=absolute, percent_change=percent)


def count_delta(current: int, previous: int) -> CountDelta:
    percent: Decimal | None = None
    if previous > 0:
        with decimal.localcontext() as ctx:
            ctx.prec = _CONTEXT_PRECISION
            percent = quantize_ratio(Decimal(current - previous) / Decimal(previous))
    return CountDelta(absolute=current - previous, percent_change=percent)


def rate_delta(current: Decimal | None, previous: Decimal | None) -> RateDelta:
    if current is None or previous is None:
        return RateDelta(absolute=None)
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return RateDelta(absolute=quantize_ratio(current - previous))


@dataclass(frozen=True)
class KpiDeltas:
    revenue: MoneyDelta
    direct_cost: MoneyDelta
    contribution: MoneyDelta
    contribution_margin: RateDelta
    completed_orders: CountDelta
    average_order_value: MoneyDelta


def compare_kpis(current: Kpis, previous: Kpis) -> KpiDeltas:
    return KpiDeltas(
        revenue=money_delta(current.revenue, previous.revenue),
        direct_cost=money_delta(current.direct_cost, previous.direct_cost),
        contribution=money_delta(current.contribution, previous.contribution),
        contribution_margin=rate_delta(current.contribution_margin, previous.contribution_margin),
        completed_orders=count_delta(current.completed_orders, previous.completed_orders),
        average_order_value=money_delta(current.average_order_value, previous.average_order_value),
    )
