"""Deterministic Dashboard classification and alert-aggregation calculators (Spec §6.13
DASH-001..005, §12.15, §14.12/§14.13, §17.2; Phase 9 approved definitions).

Pure, side-effect-free: no HTTP or database access and no `ApiError` import (ADR-109).
The Dashboard service gathers authoritative facts; this module only *classifies* them
(fulfillment risk, production timing, expiring Surplus) and *aggregates* raw findings
into the deduplicated, actionable alert list. Nothing here is persisted — alerts are
re-derived on every read (DASH-005: the Dashboard never holds independent business
truth), so there is no dismissal/acknowledgment state anywhere.

Timing rules (all times interpreted in the Business timezone, ADR-129; a missing
fulfillment time never yields an invented clock deadline, PLAN-013):

* **Fulfillment overdue** — an active (`CONFIRMED`/`READY`) Order whose Business-local
  fulfillment *date* has passed.
* **Fulfillment at risk** — a `CONFIRMED` (not yet Ready) Order with a valid fulfillment
  time whose deadline is within `fulfillment_warning_minutes` of now (or already
  passed today). Date-only Orders are never at risk; they only become overdue.
* **Production timing** — for an open requirement: `DUE_TODAY`, `ADVANCE_PREP` (the
  suggested start falls today for later fulfillment), `START_OVERDUE` (the suggested
  start — or, when no start can be computed, the demand date — has already passed with
  no run started), or `NOT_TODAY`.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.production_costing import LocalTimeValidity, validate_business_local_datetime

EXPIRING_SURPLUS_WINDOW_DAYS = 2
"""Approved Phase 9 definition: a reusable lot with available quantity whose
`usable_through_date` falls within today..today+2 (Business-local). A code constant, not
a Business setting (a setting would be a schema change)."""

ALERT_ITEM_CAP = 5
"""Maximum item rows shown per alert; the alert's `count` always carries the true total."""


class AlertSeverity(enum.StrEnum):
    WARNING = "WARNING"
    NOTICE = "NOTICE"


class AlertCode(enum.StrEnum):
    FULFILLMENT_OVERDUE = "FULFILLMENT_OVERDUE"
    FULFILLMENT_AT_RISK = "FULFILLMENT_AT_RISK"
    ORDER_LOST_READY = "ORDER_LOST_READY"
    INGREDIENT_SHORTAGE = "INGREDIENT_SHORTAGE"
    PURCHASED_PRODUCT_SHORTAGE = "PURCHASED_PRODUCT_SHORTAGE"
    PRODUCT_MISSING_RECIPE = "PRODUCT_MISSING_RECIPE"
    NEGATIVE_INGREDIENT_INVENTORY = "NEGATIVE_INGREDIENT_INVENTORY"
    MISSING_FULFILLMENT_TIME = "MISSING_FULFILLMENT_TIME"
    PRODUCTION_START_OVERDUE = "PRODUCTION_START_OVERDUE"
    SURPLUS_EXPIRING = "SURPLUS_EXPIRING"


@dataclass(frozen=True)
class _AlertSpec:
    severity: AlertSeverity
    title_singular: str
    title_plural: str
    condition: str
    impact: str
    destination_label: str
    destination_path: str


# Declaration order == presentation priority within a severity band (WARNING band first,
# then NOTICE). Each alert states the condition, its impact, and an actionable
# destination (DASH-003).
_CATALOG: dict[AlertCode, _AlertSpec] = {
    AlertCode.FULFILLMENT_OVERDUE: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} order is past its fulfillment date",
        "{count} orders are past their fulfillment date",
        "The fulfillment date has passed and the order is not completed.",
        "The customer may be waiting; fulfill or reschedule the order.",
        "Review orders",
        "/app/orders",
    ),
    AlertCode.FULFILLMENT_AT_RISK: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} order is at risk of missing its fulfillment time",
        "{count} orders are at risk of missing their fulfillment time",
        "The order is not Ready and its fulfillment time is inside the warning window.",
        "It may not be ready when the customer arrives.",
        "Review orders",
        "/app/orders",
    ),
    AlertCode.ORDER_LOST_READY: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} previously Ready order is no longer Ready",
        "{count} previously Ready orders are no longer Ready",
        "The order was Ready earlier and is now Confirmed again, so it is no longer fully covered.",
        "It needs attention before fulfillment.",
        "Review orders",
        "/app/orders",
    ),
    AlertCode.INGREDIENT_SHORTAGE: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} ingredient is short for upcoming demand",
        "{count} ingredients are short for upcoming demand",
        "Reserved demand within the shopping horizon exceeds the physical stock on hand.",
        "Production for the affected orders cannot be fully supplied without restocking.",
        "Open Shopping List",
        "/app/production/shopping-list",
    ),
    AlertCode.PURCHASED_PRODUCT_SHORTAGE: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} purchased product is short of stock",
        "{count} purchased products are short of stock",
        "Reserved quantity exceeds the physical stock of a purchased product.",
        "Affected orders cannot become Ready until stock is restocked.",
        "Review purchased inventory",
        "/app/inventory/purchased-products",
    ),
    AlertCode.PRODUCT_MISSING_RECIPE: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} product has demand but no usable recipe",
        "{count} products have demand but no usable recipe",
        "Confirmed demand exists for a produced product without an applicable recipe.",
        "Batches, ingredient needs, and shopping results cannot be calculated for it.",
        "Review products",
        "/app/products",
    ),
    AlertCode.NEGATIVE_INGREDIENT_INVENTORY: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} ingredient has a negative recorded balance",
        "{count} ingredients have a negative recorded balance",
        "Recorded physical stock is below zero after real consumption or an adjustment.",
        "Stock records need reconciliation before they can be trusted.",
        "Review ingredients",
        "/app/inventory/ingredients",
    ),
    AlertCode.MISSING_FULFILLMENT_TIME: _AlertSpec(
        AlertSeverity.WARNING,
        "{count} upcoming order has no fulfillment time",
        "{count} upcoming orders have no fulfillment time",
        "An active order within the next week has a date but no time.",
        "Suggested production start times cannot be computed for it.",
        "Review orders",
        "/app/orders",
    ),
    AlertCode.PRODUCTION_START_OVERDUE: _AlertSpec(
        AlertSeverity.NOTICE,
        "{count} production item should already have started",
        "{count} production items should already have started",
        "The suggested start time has passed, or the demand date has passed with no suggested "
        "start available, and no production run is active for it.",
        "Starting later than planned may put the fulfillment at risk (guidance only).",
        "Open Production Plan",
        "/app/production",
    ),
    AlertCode.SURPLUS_EXPIRING: _AlertSpec(
        AlertSeverity.NOTICE,
        "{count} surplus lot is nearing its usable-through date",
        "{count} surplus lots are nearing their usable-through date",
        "Available surplus will pass its seller-defined usable-through date soon.",
        "Unused surplus will no longer be eligible for new demand after that date.",
        "Review surplus",
        "/app/surplus",
    ),
}


@dataclass(frozen=True)
class AlertFinding:
    """One raw finding. `key` identifies the underlying subject (an order id, ingredient
    id, ...) so the same condition for the same subject is never listed twice."""

    code: AlertCode
    key: str
    label: str
    detail: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class AlertItem:
    label: str
    detail: str | None
    path: str | None


@dataclass(frozen=True)
class Alert:
    code: AlertCode
    severity: AlertSeverity
    title: str
    condition: str
    impact: str
    count: int
    destination_label: str
    destination_path: str
    items: list[AlertItem] = field(default_factory=list)
    items_truncated: bool = False


def aggregate_alerts(findings: Iterable[AlertFinding]) -> list[Alert]:
    """Groups findings by code into one alert each (DASH-004 / AC-DASH-003: related
    alerts aggregated, one alert per condition kind), deduplicating by `(code, key)`,
    capping listed items at `ALERT_ITEM_CAP` while reporting the true `count`, and
    ordering deterministically — WARNING before NOTICE, then the catalog's fixed priority
    order. A code with no findings produces no alert."""
    grouped: dict[AlertCode, dict[str, AlertFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.code, {}).setdefault(finding.key, finding)

    alerts: list[Alert] = []
    for code, spec in _CATALOG.items():
        by_key = grouped.get(code)
        if not by_key:
            continue
        ordered = sorted(by_key.values(), key=lambda f: (f.label, f.key))
        count = len(ordered)
        title = (spec.title_singular if count == 1 else spec.title_plural).format(count=count)
        alerts.append(
            Alert(
                code=code,
                severity=spec.severity,
                title=title,
                condition=spec.condition,
                impact=spec.impact,
                count=count,
                destination_label=spec.destination_label,
                destination_path=spec.destination_path,
                items=[AlertItem(f.label, f.detail, f.path) for f in ordered[:ALERT_ITEM_CAP]],
                items_truncated=count > ALERT_ITEM_CAP,
            )
        )

    severity_rank = {AlertSeverity.WARNING: 0, AlertSeverity.NOTICE: 1}
    # `sorted` is stable, so catalog order is preserved within a severity band.
    return sorted(alerts, key=lambda a: severity_rank[a.severity])


# --------------------------------------------------------------------------------------
# Fulfillment timing
# --------------------------------------------------------------------------------------


class FulfillmentTiming(enum.StrEnum):
    ON_TRACK = "ON_TRACK"
    AT_RISK = "AT_RISK"
    OVERDUE = "OVERDUE"


def fulfillment_deadline_utc(
    fulfillment_date: date, fulfillment_time: time | None, business_timezone: str
) -> datetime | None:
    """The fulfillment deadline as an aware UTC instant, or `None` when it cannot be
    stated honestly: no time was recorded (PLAN-013), or the Business-local wall-clock
    time does not map to exactly one instant (a DST gap/overlap — ADR-129 rejects those
    at write time, so this is purely defensive and never resolved by guessing)."""
    if fulfillment_time is None:
        return None
    validity = validate_business_local_datetime(
        local_date=fulfillment_date,
        local_time=fulfillment_time,
        business_timezone=business_timezone,
    )
    if validity is not LocalTimeValidity.VALID:
        return None
    tz = ZoneInfo(business_timezone)
    return datetime.combine(fulfillment_date, fulfillment_time).replace(tzinfo=tz).astimezone(UTC)


def classify_fulfillment(
    *,
    is_ready: bool,
    fulfillment_date: date,
    fulfillment_time: time | None,
    now_utc: datetime,
    business_today: date,
    warning_minutes: int,
    business_timezone: str,
) -> FulfillmentTiming:
    """Overdue (date passed) takes precedence; otherwise a not-Ready Order with a known
    deadline inside the warning window (inclusive boundary, and including a deadline that
    has already passed earlier today) is at risk; everything else is on track."""
    if fulfillment_date < business_today:
        return FulfillmentTiming.OVERDUE
    if is_ready:
        return FulfillmentTiming.ON_TRACK
    deadline = fulfillment_deadline_utc(fulfillment_date, fulfillment_time, business_timezone)
    if deadline is None:
        return FulfillmentTiming.ON_TRACK
    # Difference-based form of `now_utc >= deadline - warning`: identical semantics (inclusive
    # boundary) but it never subtracts a large window from a datetime, so an extreme-but-valid
    # `warning_minutes` (any positive INTEGER) cannot overflow `datetime`.
    if deadline - now_utc <= timedelta(minutes=warning_minutes):
        return FulfillmentTiming.AT_RISK
    return FulfillmentTiming.ON_TRACK


# --------------------------------------------------------------------------------------
# Production timing
# --------------------------------------------------------------------------------------


class ProductionTiming(enum.StrEnum):
    DUE_TODAY = "DUE_TODAY"
    ADVANCE_PREP = "ADVANCE_PREP"
    START_OVERDUE = "START_OVERDUE"
    NOT_TODAY = "NOT_TODAY"


def classify_production_timing(
    *,
    demand_date: date,
    suggested_start_at: datetime | None,
    now_utc: datetime,
    business_today: date,
    business_timezone: str,
) -> ProductionTiming:
    """Classifies one open (calculated, unstarted) production requirement.

    With a computed `suggested_start_at`: already passed -> `START_OVERDUE`; falls
    today -> `DUE_TODAY` when fulfillment is also today, otherwise `ADVANCE_PREP` (work
    that must begin today for a later fulfillment, Spec §14.13); falls on a later day ->
    `NOT_TODAY`. Without one (missing fulfillment time or elapsed duration — never
    fabricated), classification is date-level only: demand dated today -> `DUE_TODAY`,
    an earlier demand date -> `START_OVERDUE`, later -> `NOT_TODAY`."""
    if suggested_start_at is not None:
        if suggested_start_at < now_utc:
            return ProductionTiming.START_OVERDUE
        start_local_date = suggested_start_at.astimezone(ZoneInfo(business_timezone)).date()
        if start_local_date == business_today:
            return (
                ProductionTiming.DUE_TODAY
                if demand_date == business_today
                else ProductionTiming.ADVANCE_PREP
            )
        return ProductionTiming.NOT_TODAY

    if demand_date == business_today:
        return ProductionTiming.DUE_TODAY
    if demand_date < business_today:
        return ProductionTiming.START_OVERDUE
    return ProductionTiming.NOT_TODAY


# --------------------------------------------------------------------------------------
# Surplus expiry
# --------------------------------------------------------------------------------------


def is_surplus_expiring(
    *,
    is_reusable: bool,
    available_quantity: Decimal,
    usable_through_date: date | None,
    business_today: date,
    window_days: int = EXPIRING_SURPLUS_WINDOW_DAYS,
) -> bool:
    """A reusable lot with available (unallocated) quantity whose usable-through date is
    today or within the next `window_days` days. Undated lots never expire; an
    already-expired lot stays recorded but ineligible (SUR-007) and is not "expiring"."""
    if not is_reusable or available_quantity <= 0 or usable_through_date is None:
        return False
    return business_today <= usable_through_date <= business_today + timedelta(days=window_days)


# --------------------------------------------------------------------------------------
# Active production guidance
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunProgress:
    elapsed_minutes: int
    """Whole minutes since Start (never negative)."""
    expected_elapsed_minutes: int | None
    """`planned_batches x elapsed_minutes_per_batch_snapshot` (Spec §14.11's simple
    approximation) or `None` when the recipe has no elapsed time."""
    expected_finish_at: datetime | None
    is_past_expected: bool


def calculate_run_progress(
    *,
    started_at: datetime,
    now_utc: datetime,
    planned_batches: int,
    elapsed_minutes_per_batch: int | None,
) -> RunProgress:
    """Guidance only (PLAN-012: timing is not an optimization guarantee)."""
    elapsed = max(0, int((now_utc - started_at).total_seconds() // 60))
    if elapsed_minutes_per_batch is None:
        return RunProgress(elapsed, None, None, False)
    expected = planned_batches * elapsed_minutes_per_batch
    expected_finish_at = started_at + timedelta(minutes=expected)
    return RunProgress(elapsed, expected, expected_finish_at, now_utc > expected_finish_at)
