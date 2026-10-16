"""Response schemas for the operational Dashboard (Spec §6.13 DASH-001..005, §12.15;
Phase 9 approved definitions).

A *report* resource, not a paged collection (see `app.schemas.shopping_list`). Everything
here is derived from authoritative operational state on every read — the Dashboard owns no
data of its own (DASH-005). Customer-facing money appears only as **scheduled order
value** (never revenue); no internal cost/contribution figure is exposed (ORD-014,
ANA-004: completed-order analytics belong to Phase 10).
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel


class DashboardAlertItemResponse(BaseModel):
    label: str
    detail: str | None
    path: str | None


class DashboardAlertResponse(BaseModel):
    code: str
    severity: str
    title: str
    condition: str
    impact: str
    count: int
    destination_label: str
    destination_path: str
    items: list[DashboardAlertItemResponse]
    items_truncated: bool


class ActiveRunResponse(BaseModel):
    run_id: str
    product_id: str
    product_name: str
    planned_batches: int
    planned_output_quantity: Decimal
    started_at: datetime
    elapsed_minutes: int
    expected_elapsed_minutes: int | None
    expected_finish_at: datetime | None
    is_past_expected: bool


class ProductionItemResponse(BaseModel):
    requirement_id: str
    product_id: str
    product_name: str
    demand_date: date
    timing: str
    """`DUE_TODAY` | `ADVANCE_PREP` | `START_OVERDUE`."""
    production_demand_quantity: Decimal
    recommended_batches: int | None
    expected_output_quantity: Decimal | None
    estimated_active_minutes: int | None
    suggested_start_at: datetime | None
    has_shortage: bool
    affected_order_count: int


class ManualWorkloadResponse(BaseModel):
    """Seller-entered, clearly-manual Custom Item workload for Orders due today
    (Spec §15.14) — never disguised as a Recipe/batch projection."""

    total_minutes: int
    line_count: int
    order_count: int


class TodaysProductionResponse(BaseModel):
    items: list[ProductionItemResponse]
    manual_workload: ManualWorkloadResponse | None


class UnsatisfiedLineResponse(BaseModel):
    line_id: str
    display_name: str
    reason: str
    """`PRODUCTION_PENDING` | `PURCHASED_STOCK_SHORT` | `MANUAL_CONFIRMATION_REQUIRED` |
    `PRODUCT_UNAVAILABLE`."""


class FulfillmentItemResponse(BaseModel):
    order_id: str
    order_number: str
    customer_name: str | None
    status: str
    fulfillment_time: time | None
    fulfillment_method: str | None
    final_total: Decimal
    payment_status: str
    timing: str
    """`ON_TRACK` | `AT_RISK` | `OVERDUE`."""
    unsatisfied_lines: list[UnsatisfiedLineResponse]


class SnapshotResponse(BaseModel):
    """Operational counts only (approved Q2): no completed-order metric, no revenue."""

    orders_due_today: int
    orders_ready: int
    orders_awaiting: int
    orders_at_risk: int
    orders_overdue: int
    """All active orders past their fulfillment date (not only those due today)."""
    scheduled_order_value_today: Decimal
    """Sum of `final_total` over active orders due today — explicitly **scheduled order
    value, not revenue**."""
    production_items_today: int
    planned_batches_today: int
    active_runs: int


class UpcomingOrderResponse(BaseModel):
    order_id: str
    order_number: str
    customer_name: str | None
    status: str
    fulfillment_time: time | None


class UpcomingDayResponse(BaseModel):
    date: date
    order_count: int
    scheduled_order_value: Decimal
    orders: list[UpcomingOrderResponse]
    orders_truncated: bool
    production_item_count: int
    planned_batches: int


class DashboardResponse(BaseModel):
    generated_at: datetime
    business_today: date
    timezone: str
    attention: list[DashboardAlertResponse]
    active_production: list[ActiveRunResponse]
    todays_production: TodaysProductionResponse
    todays_fulfillments: list[FulfillmentItemResponse]
    todays_snapshot: SnapshotResponse
    upcoming: list[UpcomingDayResponse]
