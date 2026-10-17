"""Response schemas for historical Analytics (Spec §13, Phase 10).

*Report* resources (like the Dashboard / Shopping List), not generic paged collections: the
tables that grow with history (Customers, Orders) embed `items/total/limit/offset` next to
their summary. Money is a fixed 2-dp Decimal string, ratios are 6-dp Decimal strings
(0.125000 = 12.5%), quantities are Decimal strings; `null` marks an undefined value (never a
fabricated zero). Revenue is *recorded Order revenue* (operational, not accounting); Direct
Cost / Contribution / Margin are internal metrics.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class PeriodResponse(BaseModel):
    range: str
    start_date: date
    end_date: date
    days: int
    lower_bound_utc: datetime
    """Inclusive UTC instant at which `start_date` begins in the Business timezone."""
    upper_bound_utc: datetime
    """Exclusive UTC instant at which the day after `end_date` begins."""


class ReportHeader(BaseModel):
    generated_at: datetime
    business_today: date
    timezone: str
    period: PeriodResponse


# ------------------------------------------------------------------ Overview


class KpisResponse(BaseModel):
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None
    completed_orders: int
    average_order_value: Decimal | None


class MoneyDeltaResponse(BaseModel):
    absolute: Decimal | None
    percent_change: Decimal | None


class CountDeltaResponse(BaseModel):
    absolute: int
    percent_change: Decimal | None


class RateDeltaResponse(BaseModel):
    """Percentage-point (rate) difference — not a percent growth."""

    absolute: Decimal | None


class KpiDeltasResponse(BaseModel):
    revenue: MoneyDeltaResponse
    direct_cost: MoneyDeltaResponse
    contribution: MoneyDeltaResponse
    contribution_margin: RateDeltaResponse
    completed_orders: CountDeltaResponse
    average_order_value: MoneyDeltaResponse


class OverviewResponse(ReportHeader):
    current: KpisResponse
    comparison_period: PeriodResponse | None
    previous: KpisResponse | None
    deltas: KpiDeltasResponse | None
    previous_has_data: bool | None


# ------------------------------------------------------------------ Trends


class TrendBucketResponse(BaseModel):
    index: int
    label: str
    start_date: date
    end_date: date
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    completed_orders: int


class TrendsResponse(ReportHeader):
    granularity: str
    buckets: list[TrendBucketResponse]
    totals: KpisResponse


# ------------------------------------------------------------------ Products


class ProductOptionResponse(BaseModel):
    kind: str
    """`STANDARD_OPTION` or `CUSTOM_QUANTITY`."""
    selling_option_id: str | None
    name: str
    package_count: Decimal | None
    """Selling-option packages sold; `null` for Custom Quantity (no package concept)."""
    underlying_units: Decimal
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None
    completed_orders: int


class ProductRowResponse(BaseModel):
    product_id: str
    product_name: str
    product_type: str | None
    is_active: bool | None
    underlying_units: Decimal
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None
    completed_orders: int
    selling_options: list[ProductOptionResponse]


class CustomItemsResponse(BaseModel):
    line_count: int
    completed_orders: int
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None


class OrderLevelResponse(BaseModel):
    order_adjustments: Decimal
    manual_tax: Decimal
    unattributed_direct_cost: Decimal
    unreconciled_revenue: Decimal
    """Expected 0.00: `totals.revenue` minus Products, Custom Items, adjustments and tax."""


class ProductHighlightResponse(BaseModel):
    product_id: str
    product_name: str
    value: Decimal


class ProductsResponse(ReportHeader):
    rows: list[ProductRowResponse]
    custom_items: CustomItemsResponse | None
    order_level: OrderLevelResponse
    totals: KpisResponse
    most_profitable: ProductHighlightResponse | None
    """Highest total Contribution (value = Contribution)."""
    highest_margin: ProductHighlightResponse | None
    """Highest Contribution Margin among Products with Revenue (value = Margin)."""


# ------------------------------------------------------------------ Customers


class GuestSummaryResponse(BaseModel):
    completed_orders: int
    revenue: Decimal
    average_order_value: Decimal | None
    share_of_completed_orders: Decimal | None


class CustomersSummaryResponse(BaseModel):
    unique_customers: int
    new_customers: int
    returning_customers: int
    guest: GuestSummaryResponse


class CustomerRowResponse(BaseModel):
    customer_id: str
    customer_name: str
    is_active: bool | None
    classification: str
    """`NEW` or `RETURNING`."""
    completed_orders: int
    revenue: Decimal
    average_order_value: Decimal | None


class CustomersResponse(ReportHeader):
    summary: CustomersSummaryResponse
    items: list[CustomerRowResponse]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ Orders


class FulfillmentMethodResponse(BaseModel):
    method: str
    """`PICKUP`, `DELIVERY`, `OTHER` or `UNSPECIFIED`."""
    completed_orders: int
    revenue: Decimal


class LargestOrderResponse(BaseModel):
    order_id: str
    order_number: str
    final_total: Decimal
    completed_at: datetime
    customer_name: str | None


class OutstandingResponse(BaseModel):
    label: str
    """Always `Outstanding recorded balance — as of now` (payments are as-of-request)."""
    order_count: int
    balance: Decimal


class OrdersSummaryResponse(BaseModel):
    completed_orders: int
    canceled_orders: int
    revenue: Decimal
    average_order_value: Decimal | None
    largest_completed_order: LargestOrderResponse | None
    fulfillment_methods: list[FulfillmentMethodResponse]
    outstanding: OutstandingResponse


class OrderRowResponse(BaseModel):
    order_id: str
    order_number: str
    status: str
    reported_at: datetime
    """`completed_at` for Completed Orders, `canceled_at` for Canceled Orders."""
    customer_id: str | None
    customer_name: str | None
    fulfillment_method: str | None
    final_total: Decimal
    payments_total: Decimal
    payment_status: str | None
    outstanding_balance: Decimal | None
    direct_cost: Decimal | None
    contribution: Decimal | None


class OrdersResponse(ReportHeader):
    summary: OrdersSummaryResponse
    items: list[OrderRowResponse]
    total: int
    limit: int
    offset: int
