"""Historical Analytics endpoints (Spec §6.14, §13; Phase 10). Thin router per
`CLAUDE.md`/Spec §11.2 — all querying lives in `app.services.analytics_service`, all period
arithmetic in `app.domain.analytics_periods`. Read-only: GET only, no CSRF requirement, no
locks, no writes. The Business is derived solely from the authenticated session (SEC-002); no
`business_id` parameter exists (an unknown extra query parameter is simply ignored)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_business
from app.core import csv_export
from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.session import get_db
from app.domain import analytics_kpis as kpi
from app.domain.analytics_periods import AnalyticsRange, Period, ResolvedPeriods, resolve_periods
from app.schemas import analytics as s
from app.services import analytics_csv as csv_rows
from app.services import analytics_service as svc

router = APIRouter()

OUTSTANDING_LABEL = "Outstanding recorded balance — as of now"


def _utc_now() -> datetime:
    """The request clock — a module-level function so tests can freeze time
    deterministically (Spec §19.13) without touching the service."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class RangeParams:
    range: AnalyticsRange
    start_date: date | None
    end_date: date | None


def range_params(
    range_: Annotated[AnalyticsRange, Query(alias="range")] = AnalyticsRange.LAST_30_DAYS,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
) -> RangeParams:
    return RangeParams(range=range_, start_date=start_date, end_date=end_date)


@dataclass(frozen=True)
class Resolved:
    business: Business
    periods: ResolvedPeriods
    tz: ZoneInfo
    now_utc: datetime
    business_today: date

    @property
    def period(self) -> Period:
        return self.periods.current

    def header(self) -> dict:
        return {
            "generated_at": self.now_utc,
            "business_today": self.business_today,
            "timezone": self.business.timezone,
            "period": _period(self.periods.range, self.periods.current),
        }


def _period(range_: AnalyticsRange, p: Period) -> s.PeriodResponse:
    return s.PeriodResponse(
        range=range_.value,
        start_date=p.start_date,
        end_date=p.end_date,
        days=p.days,
        lower_bound_utc=p.lower_bound,
        upper_bound_utc=p.upper_bound,
    )


def _resolve(business: Business, params: RangeParams, *, compare: bool) -> Resolved:
    now_utc = _utc_now()
    tz = ZoneInfo(business.timezone)
    today = now_utc.astimezone(tz).date()
    periods, issues = resolve_periods(
        range_=params.range,
        start_date=params.start_date,
        end_date=params.end_date,
        business_today=today,
        tz=tz,
        compare=compare,
    )
    if periods is None:
        raise ApiError(
            422,
            "ANALYTICS_RANGE_INVALID",
            "The requested Analytics range is not valid.",
            issues=[
                {
                    "severity": "ERROR",
                    "code": i.code,
                    "message": i.message,
                    "field": i.field,
                    "resource": None,
                    "details": i.details or {},
                }
                for i in issues
            ],
        )
    return Resolved(business, periods, tz, now_utc, today)


def selected_period(
    business: Annotated[Business, Depends(get_current_business)],
    params: Annotated[RangeParams, Depends(range_params)],
) -> Resolved:
    return _resolve(business, params, compare=False)


def overview_periods(
    business: Annotated[Business, Depends(get_current_business)],
    params: Annotated[RangeParams, Depends(range_params)],
    compare: Annotated[bool, Query()] = False,
) -> Resolved:
    return _resolve(business, params, compare=compare)


# ------------------------------------------------------------------ mappers


def _kpis(k: kpi.Kpis) -> s.KpisResponse:
    return s.KpisResponse(
        revenue=k.revenue,
        direct_cost=k.direct_cost,
        contribution=k.contribution,
        contribution_margin=k.contribution_margin,
        completed_orders=k.completed_orders,
        average_order_value=k.average_order_value,
    )


def _money_delta(d: kpi.MoneyDelta) -> s.MoneyDeltaResponse:
    return s.MoneyDeltaResponse(absolute=d.absolute, percent_change=d.percent_change)


def _deltas(d: kpi.KpiDeltas) -> s.KpiDeltasResponse:
    return s.KpiDeltasResponse(
        revenue=_money_delta(d.revenue),
        direct_cost=_money_delta(d.direct_cost),
        contribution=_money_delta(d.contribution),
        contribution_margin=s.RateDeltaResponse(absolute=d.contribution_margin.absolute),
        completed_orders=s.CountDeltaResponse(
            absolute=d.completed_orders.absolute, percent_change=d.completed_orders.percent_change
        ),
        average_order_value=_money_delta(d.average_order_value),
    )


def _product_option(o: svc.ProductOptionRow) -> s.ProductOptionResponse:
    return s.ProductOptionResponse(
        kind=o.kind.value,
        selling_option_id=str(o.selling_option_id) if o.selling_option_id else None,
        name=o.name,
        package_count=o.package_count,
        underlying_units=o.underlying_units,
        revenue=o.revenue,
        direct_cost=o.direct_cost,
        contribution=o.contribution,
        contribution_margin=o.contribution_margin,
        completed_orders=o.completed_orders,
    )


def _highlight(h: svc.ProductHighlight | None) -> s.ProductHighlightResponse | None:
    if h is None:
        return None
    return s.ProductHighlightResponse(
        product_id=str(h.product_id), product_name=h.name, value=h.value
    )


def _customer(c: svc.CustomerRow) -> s.CustomerRowResponse:
    return s.CustomerRowResponse(
        customer_id=str(c.customer_id),
        customer_name=c.name,
        is_active=c.is_active,
        classification=c.classification.value,
        completed_orders=c.completed_orders,
        revenue=c.revenue,
        average_order_value=c.average_order_value,
    )


def _guest(g: svc.GuestSummary) -> s.GuestSummaryResponse:
    return s.GuestSummaryResponse(
        completed_orders=g.completed_orders,
        revenue=g.revenue,
        average_order_value=g.average_order_value,
        share_of_completed_orders=g.share_of_completed_orders,
    )


def _order(o: svc.OrderRow) -> s.OrderRowResponse:
    return s.OrderRowResponse(
        order_id=str(o.order_id),
        order_number=o.order_number,
        status=o.status.value,
        reported_at=o.reported_at,
        customer_id=str(o.customer_id) if o.customer_id else None,
        customer_name=o.customer_name,
        fulfillment_method=o.fulfillment_method.value if o.fulfillment_method else None,
        final_total=o.final_total,
        payments_total=o.payments_total,
        payment_status=o.payment_status.value if o.payment_status else None,
        outstanding_balance=o.outstanding_balance,
        direct_cost=o.direct_cost,
        contribution=o.contribution,
    )


# ------------------------------------------------------------------ JSON endpoints


@router.get("/overview")
def get_overview(
    ctx: Annotated[Resolved, Depends(overview_periods)],
    db: Annotated[Session, Depends(get_db)],
) -> s.OverviewResponse:
    report = svc.get_overview(db, ctx.business, ctx.periods)
    previous = ctx.periods.previous
    return s.OverviewResponse(
        **ctx.header(),
        current=_kpis(report.current),
        comparison_period=_period(ctx.periods.range, previous) if previous else None,
        previous=_kpis(report.previous) if report.previous else None,
        deltas=_deltas(report.deltas) if report.deltas else None,
        previous_has_data=report.previous_has_data,
    )


@router.get("/trends")
def get_trends(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
) -> s.TrendsResponse:
    report = svc.get_trends(db, ctx.business, ctx.period, ctx.tz)
    return s.TrendsResponse(
        **ctx.header(),
        granularity=report.granularity.value,
        buckets=[
            s.TrendBucketResponse(
                index=r.bucket.index,
                label=r.bucket.label,
                start_date=r.bucket.start_date,
                end_date=r.bucket.end_date,
                revenue=r.revenue,
                direct_cost=r.direct_cost,
                contribution=r.contribution,
                completed_orders=r.completed_orders,
            )
            for r in report.buckets
        ],
        totals=_kpis(report.totals),
    )


@router.get("/products")
def get_products(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
    sort: svc.ProductSort = svc.ProductSort.CONTRIBUTION,
    direction: svc.SortDirection | None = None,
) -> s.ProductsResponse:
    report = svc.get_products(db, ctx.business, ctx.period, sort=sort, direction=direction)
    custom = report.custom_items
    return s.ProductsResponse(
        **ctx.header(),
        rows=[
            s.ProductRowResponse(
                product_id=str(p.product_id),
                product_name=p.name,
                product_type=p.product_type,
                is_active=p.is_active,
                underlying_units=p.underlying_units,
                revenue=p.revenue,
                direct_cost=p.direct_cost,
                contribution=p.contribution,
                contribution_margin=p.contribution_margin,
                completed_orders=p.completed_orders,
                selling_options=[_product_option(o) for o in p.selling_options],
            )
            for p in report.rows
        ],
        custom_items=s.CustomItemsResponse(
            line_count=custom.line_count,
            completed_orders=custom.completed_orders,
            revenue=custom.revenue,
            direct_cost=custom.direct_cost,
            contribution=custom.contribution,
            contribution_margin=custom.contribution_margin,
        )
        if custom
        else None,
        order_level=s.OrderLevelResponse(
            order_adjustments=report.order_level.order_adjustments,
            manual_tax=report.order_level.manual_tax,
            unattributed_direct_cost=report.order_level.unattributed_direct_cost,
            unreconciled_revenue=report.order_level.unreconciled_revenue,
        ),
        totals=_kpis(report.totals),
        most_profitable=_highlight(report.most_profitable),
        highest_margin=_highlight(report.highest_margin),
    )


@router.get("/customers")
def get_customers(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
    sort: svc.CustomerSort = svc.CustomerSort.REVENUE,
    direction: svc.SortDirection | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> s.CustomersResponse:
    report = svc.get_customers(
        db, ctx.business, ctx.period, sort=sort, direction=direction, limit=limit, offset=offset
    )
    return s.CustomersResponse(
        **ctx.header(),
        summary=s.CustomersSummaryResponse(
            unique_customers=report.summary.unique_customers,
            new_customers=report.summary.new_customers,
            returning_customers=report.summary.returning_customers,
            guest=_guest(report.summary.guest),
        ),
        items=[_customer(c) for c in report.items],
        total=report.total,
        limit=report.limit,
        offset=report.offset,
    )


@router.get("/orders")
def get_orders(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
    status: svc.OrderStatusFilter = svc.OrderStatusFilter.ALL,
    outstanding_only: bool = False,
    sort: svc.OrderSort = svc.OrderSort.REPORTED_AT,
    direction: svc.SortDirection | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> s.OrdersResponse:
    report = svc.get_orders(
        db,
        ctx.business,
        ctx.period,
        status=status,
        outstanding_only=outstanding_only,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    summary = report.summary
    largest = summary.largest_completed_order
    return s.OrdersResponse(
        **ctx.header(),
        summary=s.OrdersSummaryResponse(
            completed_orders=summary.completed_orders,
            canceled_orders=summary.canceled_orders,
            revenue=summary.revenue,
            average_order_value=summary.average_order_value,
            largest_completed_order=s.LargestOrderResponse(
                order_id=str(largest.order_id),
                order_number=largest.order_number,
                final_total=largest.final_total,
                completed_at=largest.completed_at,
                customer_name=largest.customer_name,
            )
            if largest
            else None,
            fulfillment_methods=[
                s.FulfillmentMethodResponse(
                    method=m.method.value if m.method else "UNSPECIFIED",
                    completed_orders=m.completed_orders,
                    revenue=m.revenue,
                )
                for m in summary.fulfillment_methods
            ],
            outstanding=s.OutstandingResponse(
                label=OUTSTANDING_LABEL,
                order_count=summary.outstanding.order_count,
                balance=summary.outstanding.balance,
            ),
        ),
        items=[_order(o) for o in report.items],
        total=report.total,
        limit=report.limit,
        offset=report.offset,
    )


# ------------------------------------------------------------------ CSV endpoints


def _csv_response(name: str, ctx: Resolved, columns: list[str], rows) -> StreamingResponse:
    start, end = ctx.period.start_date.isoformat(), ctx.period.end_date.isoformat()
    filename = f"analytics-{name}_{start}_to_{end}.csv"
    return StreamingResponse(
        csv_export.stream_csv(columns, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/products.csv")
def export_products_csv(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
    sort: svc.ProductSort = svc.ProductSort.CONTRIBUTION,
    direction: svc.SortDirection | None = None,
) -> StreamingResponse:
    report = svc.get_products(db, ctx.business, ctx.period, sort=sort, direction=direction)
    return _csv_response("products", ctx, csv_rows.PRODUCT_COLUMNS, csv_rows.product_rows(report))


@router.get("/trends.csv")
def export_trends_csv(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
) -> StreamingResponse:
    report = svc.get_trends(db, ctx.business, ctx.period, ctx.tz)
    return _csv_response("trends", ctx, csv_rows.TREND_COLUMNS, csv_rows.trend_rows(report))


@router.get("/customers.csv")
def export_customers_csv(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
    sort: svc.CustomerSort = svc.CustomerSort.REVENUE,
    direction: svc.SortDirection | None = None,
) -> StreamingResponse:
    stream = svc.stream_customer_export(
        db, ctx.business, ctx.period, sort=sort, direction=direction
    )
    return _csv_response(
        "customers", ctx, csv_rows.CUSTOMER_COLUMNS, csv_rows.customer_rows(stream)
    )


@router.get("/orders.csv")
def export_orders_csv(
    ctx: Annotated[Resolved, Depends(selected_period)],
    db: Annotated[Session, Depends(get_db)],
    status: svc.OrderStatusFilter = svc.OrderStatusFilter.ALL,
    outstanding_only: bool = False,
    sort: svc.OrderSort = svc.OrderSort.REPORTED_AT,
    direction: svc.SortDirection | None = None,
) -> StreamingResponse:
    stream = svc.stream_orders(
        db,
        ctx.business,
        ctx.period,
        status=status,
        outstanding_only=outstanding_only,
        sort=sort,
        direction=direction,
    )
    return _csv_response("orders", ctx, csv_rows.ORDER_COLUMNS, csv_rows.order_rows(stream))
