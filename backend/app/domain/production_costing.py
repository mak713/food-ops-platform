"""Deterministic planned-cost, workload, and suggested-start calculators (Spec
§7.6/§7.10/§14.11/§14.12; Phase 7 Plan v2 §4/§13, corrected by Final Pre-Implementation
Amendment §1/§2 and Final Architecture Lock §G).

Pure, side-effect-free: no HTTP or database access, no `ApiError` import (ADR-109;
Final Architecture Lock §G explicitly forbids importing `ApiError` into this module —
`calculate_suggested_start` reports a structured, non-raising `SuggestedStartResult`
instead, which the service layer alone translates into a rejection/rollback). All
Decimal arithmetic runs inside an explicit, fixed-precision `decimal.localcontext()`.
"""

from __future__ import annotations

import decimal
import enum
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.inventory_costing import resolve_effective_replacement_cost

_CONTEXT_PRECISION = 50


def resolve_planned_ingredient_unit_cost(
    *,
    physical_quantity: Decimal,
    weighted_average_unit_cost: Decimal,
    replacement_unit_cost: Decimal | None,
    latest_purchase_unit_cost: Decimal | None,
) -> Decimal | None:
    """WAC-first planned-cost basis (Final Pre-Implementation Amendment §1):

    - `physical_quantity > 0` -> `weighted_average_unit_cost` (even a legitimate `0`).
    - `physical_quantity <= 0` -> `resolve_effective_replacement_cost` (ADR-112,
      reused directly, not re-derived).
    - If that fallback is also `None`, the basis is unknown -> returns `None`. The
      caller must leave the affected estimated-cost fields `NULL`, never substitute a
      fabricated `0`.
    """
    if physical_quantity > 0:
        return weighted_average_unit_cost
    return resolve_effective_replacement_cost(replacement_unit_cost, latest_purchase_unit_cost)


def calculate_ingredient_planned_total_cost(
    *, required_quantity_canonical: Decimal, unit_cost: Decimal
) -> Decimal:
    """`total_cost = required_quantity_canonical × unit_cost` (Spec §13/§7.10 planned
    ingredient cost) — isolated in its own `decimal.localcontext()` (ADR-109) so this
    single multiply, used per-ingredient inside the recalculation core, never runs
    under the caller's ambient global Decimal context."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return required_quantity_canonical * unit_cost


def calculate_planned_labor_cost(
    *, batches: int, active_minutes_per_batch: int, labor_rate: Decimal
) -> Decimal:
    """`labor_cost = batches × active_minutes_per_batch × labor_rate / 60` (Spec §7.6).
    Always computable — `Business.default_labor_rate` is `NOT NULL`."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return Decimal(batches) * Decimal(active_minutes_per_batch) * labor_rate / Decimal(60)


@dataclass(frozen=True)
class WorkloadResult:
    estimated_active_minutes: int
    estimated_elapsed_minutes: int | None


def calculate_workload(
    *, batches: int, active_minutes_per_batch: int, elapsed_minutes_per_batch: int | None
) -> WorkloadResult:
    """`estimated_active_minutes = batches × active_minutes_per_batch`;
    `estimated_elapsed_minutes = batches × elapsed_minutes_per_batch` when known
    (Spec §14.11). Returns plain, unbounded `int`s — the service layer is responsible
    for the `INT32` representability guard before persistence."""
    estimated_active = batches * active_minutes_per_batch
    estimated_elapsed = (
        batches * elapsed_minutes_per_batch if elapsed_minutes_per_batch is not None else None
    )
    return WorkloadResult(
        estimated_active_minutes=estimated_active, estimated_elapsed_minutes=estimated_elapsed
    )


class SuggestedStartStatus(enum.StrEnum):
    OK = "OK"
    #: No fulfillment time and/or no elapsed duration — a normal, non-error outcome
    #: (Spec §14.12: "must not invent a precise clock deadline").
    MISSING_INPUT = "MISSING_INPUT"
    #: The local wall-clock time does not exist for `business_timezone` on that date
    #: (a DST "spring-forward" gap).
    NONEXISTENT_LOCAL_TIME = "NONEXISTENT_LOCAL_TIME"
    #: The local wall-clock time occurs twice for `business_timezone` on that date (a
    #: DST "fall-back" overlap) — never silently resolved by picking one occurrence.
    AMBIGUOUS_LOCAL_TIME = "AMBIGUOUS_LOCAL_TIME"
    #: All inputs were present and individually valid, but the derived duration is so
    #: large the datetime arithmetic itself overflows — a data-integrity anomaly, not
    #: a missing-input case.
    ARITHMETIC_OVERFLOW = "ARITHMETIC_OVERFLOW"


@dataclass(frozen=True)
class SuggestedStartResult:
    status: SuggestedStartStatus
    #: Only set when `status is SuggestedStartStatus.OK` — an aware UTC datetime.
    suggested_start_at_utc: datetime | None


class LocalTimeValidity(enum.StrEnum):
    VALID = "VALID"
    #: The local wall-clock time does not exist for `business_timezone` on that date
    #: (a DST "spring-forward" gap).
    NONEXISTENT = "NONEXISTENT"
    #: The local wall-clock time occurs twice for `business_timezone` on that date (a
    #: DST "fall-back" overlap) — never silently resolved by picking one occurrence.
    AMBIGUOUS = "AMBIGUOUS"


def validate_business_local_datetime(
    *, local_date: date, local_time: time, business_timezone: str
) -> LocalTimeValidity:
    """Pure Business-local wall-clock DST validity check (Phase 7 Final Lifecycle
    Invariant Correction Plan, Finding C), extracted from `calculate_suggested_start`
    so wall-clock validity can be checked independently of Recipe/elapsed-time
    availability, and by callers with no "suggested start" concept at all
    (Purchased-only/Custom-Item-only Orders; Order confirmation/edit/Preview header
    validation; the recalculation layer's own per-contributing-time validation).
    Detects a nonexistent (spring-forward gap) or ambiguous (fall-back overlap) local
    wall-clock deterministically via a `ZoneInfo` round-trip / `fold=0`/`fold=1`
    comparison — never silently resolved."""
    tz = ZoneInfo(business_timezone)
    naive_local = datetime.combine(local_date, local_time)
    local_dt = naive_local.replace(tzinfo=tz)

    # Nonexistent-local-time detection: round-trip through UTC and back. If the
    # local wall-clock does not survive the round trip unchanged, it fell inside a
    # spring-forward gap and never actually occurred.
    utc_dt = local_dt.astimezone(UTC)
    roundtrip_local = utc_dt.astimezone(tz)
    if roundtrip_local.replace(tzinfo=None) != naive_local:
        return LocalTimeValidity.NONEXISTENT

    # Ambiguous-local-time detection: the same local wall-clock/zone pair resolves to
    # two different UTC instants depending on `fold` only during a fall-back overlap.
    fold0_utc = naive_local.replace(tzinfo=tz, fold=0).astimezone(UTC)
    fold1_utc = naive_local.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if fold0_utc != fold1_utc:
        return LocalTimeValidity.AMBIGUOUS

    return LocalTimeValidity.VALID


def calculate_suggested_start(
    *,
    fulfillment_date: date,
    fulfillment_time: time | None,
    elapsed_minutes: int | None,
    business_timezone: str,
) -> SuggestedStartResult:
    """`suggested_start = fulfillment_deadline - estimated_elapsed_duration` (Spec
    §14.12), constructed in Business-local time and returned as an aware UTC instant.
    Missing time or missing elapsed duration never fabricates a deadline. A
    nonexistent or ambiguous local wall-clock time is detected deterministically
    (never silently guessed) and reported, not raised — see module docstring.

    Wall-clock DST validity (Phase 7 Final Lifecycle Invariant Correction Plan,
    Finding C) is checked independently of `elapsed_minutes` availability — a
    genuinely supplied but invalid time is never masked by a missing elapsed
    duration; only once the wall-clock is confirmed valid does a missing elapsed
    duration fall back to `MISSING_INPUT`."""
    if fulfillment_time is None:
        return SuggestedStartResult(SuggestedStartStatus.MISSING_INPUT, None)

    validity = validate_business_local_datetime(
        local_date=fulfillment_date,
        local_time=fulfillment_time,
        business_timezone=business_timezone,
    )
    if validity is LocalTimeValidity.NONEXISTENT:
        return SuggestedStartResult(SuggestedStartStatus.NONEXISTENT_LOCAL_TIME, None)
    if validity is LocalTimeValidity.AMBIGUOUS:
        return SuggestedStartResult(SuggestedStartStatus.AMBIGUOUS_LOCAL_TIME, None)

    if elapsed_minutes is None:
        return SuggestedStartResult(SuggestedStartStatus.MISSING_INPUT, None)

    tz = ZoneInfo(business_timezone)
    naive_local = datetime.combine(fulfillment_date, fulfillment_time)
    utc_dt = naive_local.replace(tzinfo=tz).astimezone(UTC)
    try:
        suggested_start_utc = utc_dt - timedelta(minutes=elapsed_minutes)
    except OverflowError:
        return SuggestedStartResult(SuggestedStartStatus.ARITHMETIC_OVERFLOW, None)

    return SuggestedStartResult(SuggestedStartStatus.OK, suggested_start_utc)
