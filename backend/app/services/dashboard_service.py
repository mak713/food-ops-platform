"""Operational Dashboard assembly (Spec §6.13 DASH-001..005, §12.15; ADR-069; Phase 9).

Strictly a **read-side orchestration** layer: it gathers authoritative operational facts
with tenant-scoped queries, hands them to the pure calculators in
`app.domain.dashboard_alerts` / `app.domain.shopping_list`, and shapes the response. It
owns no data (DASH-005), persists no alert/dismissal state, takes **no row locks** and
performs **no writes** — so it can never enter, extend, or perturb the Phase 7/8 lock
hierarchy (ADR-124/141). The clock is injected (`now_utc`, `business_today`) so the
timezone-boundary behavior is deterministically testable (Spec §19.13).

Read-consistency approach: the Dashboard issues a bounded number of tenant-scoped,
lock-free read queries. Shared pure calculators (the same ADR-130 shortage formula, the
same risk/timing classifiers) give the sections *semantic* consistency — the same rule is
applied wherever a condition appears. There is **no request-level snapshot guarantee**:
queries run under the default READ COMMITTED isolation, so a transaction that commits
between two of this request's queries can be observed by the later one, producing a
momentary cross-section skew that the next refetch resolves. No partially committed Phase 8
workflow state is ever exposed, because those workflows write transactionally (each commits
atomically). This is a deliberate, documented limitation for a display-only read path.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.api_errors import ApiError
from app.db.enums import (
    CalculationStatus,
    OrderLineType,
    OrderStatus,
    ProductionRunStatus,
)
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order, OrderLine, OrderStatusHistory, Payment
from app.db.models.product import Product
from app.db.models.production import ProductionRequirement, ProductionRun
from app.db.models.surplus import PurchasedProductReservation, SurplusInventory
from app.domain import dashboard_alerts as alerts_domain
from app.domain import order_pricing
from app.domain.dashboard_alerts import (
    AlertCode,
    AlertFinding,
    FulfillmentTiming,
    ProductionTiming,
)
from app.domain.inventory_availability import calculate_available
from app.schemas.dashboard import (
    ActiveRunResponse,
    DashboardAlertItemResponse,
    DashboardAlertResponse,
    DashboardResponse,
    FulfillmentItemResponse,
    ManualWorkloadResponse,
    ProductionItemResponse,
    SnapshotResponse,
    TodaysProductionResponse,
    UnsatisfiedLineResponse,
    UpcomingDayResponse,
    UpcomingOrderResponse,
)
from app.services import (
    availability_service,
    production_run_service,
    readiness_service,
    shopping_list_service,
)
from app.services.requirement_insight_service import build_requirement_insights

UPCOMING_DAYS = 7
"""Approved Q2: Upcoming = the seven Business-local dates after today."""

UPCOMING_ORDERS_PER_DAY_CAP = 10

REQUIREMENT_PAGE_SIZE = 500
"""Chunk size used to exhaust the paginated Phase 8 requirement reader. It bounds each
query, never the result: every actionable requirement is loaded (no silent ceiling)."""

_MAX_PAGING_PASSES = 3

_ORDERS_PATH = "/app/orders"


def load_all_pages[T](
    reader: Callable[[int, int], tuple[list[T], int]],
    *,
    key: Callable[[T], Hashable],
    page_size: int,
) -> list[T]:
    """Exhausts an existing `(limit, offset) -> (items, total)` paginated reader in chunks
    and returns every item — no fixed result-size ceiling, and never a silently truncated or
    knowingly incomplete result.

    Correctness rests on the reader having a **deterministic total order** (the Phase 8
    requirement reader orders by `(demand_date, id)`): under stable data, OFFSET paging then
    visits every row exactly once, even rows tied on the leading sort key. Reading ends at
    the first short (or empty) page, so an exact multiple of `page_size` terminates on the
    following empty page. Items are de-duplicated by `key` only as a defensive measure.

    After each pass the distinct count is compared with the reader's reported `total`. A
    mismatch can only arise from a concurrent commit between queries (READ COMMITTED), so
    the whole read is repeated, up to `_MAX_PAGING_PASSES` passes. If the data is still
    moving after that, the read is **rejected** with a retryable `409` rather than returned
    as if it were complete."""
    for _ in range(_MAX_PAGING_PASSES):
        collected: dict[Hashable, T] = {}
        offset = 0
        total = 0
        while True:
            items, total = reader(page_size, offset)
            for item in items:
                collected.setdefault(key(item), item)
            if len(items) < page_size:
                break
            offset += page_size
        if len(collected) == total:
            return list(collected.values())
    raise ApiError(
        409,
        "DASHBOARD_READ_INCONSISTENT",
        "The dashboard data changed while it was being read. Please retry.",
    )


def _fmt(quantity: Decimal) -> str:
    """Human-readable quantity: trailing zeros trimmed (`60.000000` -> `60`)."""
    text = format(quantity.normalize(), "f")
    return text


@dataclass(frozen=True)
class _ActiveOrder:
    id: uuid.UUID
    order_number: str
    status: OrderStatus
    fulfillment_date: date
    fulfillment_time: time | None
    fulfillment_method: str | None
    final_total: Decimal
    customer_name: str | None

    @property
    def customer_label(self) -> str:
        return self.customer_name or "Guest"


def _load_active_orders(db: Session, business: Business) -> list[_ActiveOrder]:
    """Every CONFIRMED/READY Order with a fulfillment date (always present once
    confirmed — ADR-123 keeps a persisted CONFIRMED Order structurally confirmable)."""
    rows = db.execute(
        select(
            Order.id,
            Order.order_number,
            Order.status,
            Order.fulfillment_date,
            Order.fulfillment_time,
            Order.fulfillment_method,
            Order.final_total,
            Customer.name,
        )
        .outerjoin(
            Customer, (Customer.id == Order.customer_id) & (Customer.business_id == business.id)
        )
        .where(
            Order.business_id == business.id,
            Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.READY)),
            Order.fulfillment_date.is_not(None),
        )
        .order_by(Order.fulfillment_date.asc(), Order.fulfillment_time.asc().nulls_last())
    ).all()
    return [
        _ActiveOrder(
            id=r[0],
            order_number=r[1],
            status=r[2],
            fulfillment_date=r[3],
            fulfillment_time=r[4],
            fulfillment_method=r[5].value if r[5] is not None else None,
            final_total=r[6],
            customer_name=r[7],
        )
        for r in rows
    ]


def _time_sort_key(order: _ActiveOrder) -> tuple:
    return (order.fulfillment_time is None, order.fulfillment_time or time.min, order.order_number)


def build_dashboard(
    db: Session, business: Business, *, now_utc: datetime, business_today: date
) -> DashboardResponse:
    tz = business.timezone
    findings: list[AlertFinding] = []

    # ---------------------------------------------------------------- Orders
    active_orders = _load_active_orders(db, business)
    timing_by_order: dict[uuid.UUID, FulfillmentTiming] = {}
    for order in active_orders:
        timing = alerts_domain.classify_fulfillment(
            is_ready=order.status is OrderStatus.READY,
            fulfillment_date=order.fulfillment_date,
            fulfillment_time=order.fulfillment_time,
            now_utc=now_utc,
            business_today=business_today,
            warning_minutes=business.fulfillment_warning_minutes,
            business_timezone=tz,
        )
        timing_by_order[order.id] = timing
        order_path = f"{_ORDERS_PATH}/{order.id}"
        if timing is FulfillmentTiming.OVERDUE:
            findings.append(
                AlertFinding(
                    AlertCode.FULFILLMENT_OVERDUE,
                    str(order.id),
                    order.order_number,
                    f"{order.customer_label} · was due {order.fulfillment_date.isoformat()}",
                    order_path,
                )
            )
        elif timing is FulfillmentTiming.AT_RISK:
            findings.append(
                AlertFinding(
                    AlertCode.FULFILLMENT_AT_RISK,
                    str(order.id),
                    order.order_number,
                    f"{order.customer_label} · due {order.fulfillment_date.isoformat()} "
                    f"{order.fulfillment_time:%H:%M}",
                    order_path,
                )
            )
        if (
            order.fulfillment_time is None
            and business_today
            <= order.fulfillment_date
            <= business_today + timedelta(days=UPCOMING_DAYS)
        ):
            findings.append(
                AlertFinding(
                    AlertCode.MISSING_FULFILLMENT_TIME,
                    str(order.id),
                    order.order_number,
                    f"{order.customer_label} · {order.fulfillment_date.isoformat()}",
                    order_path,
                )
            )

    lost_ready_rows = db.execute(
        select(Order.id, Order.order_number, Customer.name)
        .outerjoin(
            Customer, (Customer.id == Order.customer_id) & (Customer.business_id == business.id)
        )
        .where(
            Order.business_id == business.id,
            Order.status == OrderStatus.CONFIRMED,
            exists().where(
                OrderStatusHistory.order_id == Order.id,
                OrderStatusHistory.business_id == business.id,
                OrderStatusHistory.to_status == OrderStatus.READY,
            ),
        )
    ).all()
    for order_id, order_number, customer_name in lost_ready_rows:
        findings.append(
            AlertFinding(
                AlertCode.ORDER_LOST_READY,
                str(order_id),
                order_number,
                f"{customer_name or 'Guest'} · was Ready earlier, now Confirmed again",
                f"{_ORDERS_PATH}/{order_id}",
            )
        )

    # --------------------------------------------------- Production requirements
    open_requirements = load_all_pages(
        lambda limit, offset: production_run_service.list_production_requirements_for_business(
            db, business, limit=limit, offset=offset
        ),
        key=lambda requirement: requirement.id,
        page_size=REQUIREMENT_PAGE_SIZE,
    )
    insights = build_requirement_insights(db, business, open_requirements)

    production_items: list[ProductionItemResponse] = []
    upcoming_production: dict[date, list[ProductionRequirement]] = {}
    for requirement in open_requirements:
        timing = alerts_domain.classify_production_timing(
            demand_date=requirement.demand_date,
            suggested_start_at=requirement.suggested_start_at,
            now_utc=now_utc,
            business_today=business_today,
            business_timezone=tz,
        )
        if (
            business_today
            < requirement.demand_date
            <= business_today + timedelta(days=UPCOMING_DAYS)
        ):
            upcoming_production.setdefault(requirement.demand_date, []).append(requirement)
        if timing is ProductionTiming.NOT_TODAY:
            continue
        insight = insights.get(requirement.id)
        production_items.append(
            ProductionItemResponse(
                requirement_id=str(requirement.id),
                product_id=str(requirement.product_id),
                product_name=requirement.product.name,
                demand_date=requirement.demand_date,
                timing=timing.value,
                production_demand_quantity=requirement.production_demand_quantity,
                recommended_batches=requirement.recommended_batches,
                expected_output_quantity=requirement.expected_output_quantity,
                estimated_active_minutes=requirement.estimated_active_minutes,
                suggested_start_at=requirement.suggested_start_at,
                has_shortage=insight.has_shortage if insight else False,
                affected_order_count=len(insight.affected_orders) if insight else 0,
            )
        )
        if timing is ProductionTiming.START_OVERDUE:
            detail = (
                "suggested start time has passed"
                if requirement.suggested_start_at is not None
                else f"demand date {requirement.demand_date.isoformat()} has passed"
            )
            findings.append(
                AlertFinding(
                    AlertCode.PRODUCTION_START_OVERDUE,
                    str(requirement.id),
                    requirement.product.name,
                    detail,
                    "/app/production",
                )
            )

    timing_rank = {
        ProductionTiming.START_OVERDUE.value: 0,
        ProductionTiming.DUE_TODAY.value: 1,
        ProductionTiming.ADVANCE_PREP.value: 2,
    }
    production_items.sort(
        key=lambda i: (
            timing_rank[i.timing],
            i.suggested_start_at is None,
            i.suggested_start_at or now_utc,
            i.product_name.casefold(),
            i.requirement_id,
        )
    )

    incomplete_rows = db.execute(
        select(
            ProductionRequirement.product_id,
            Product.name,
            func.min(ProductionRequirement.demand_date),
            func.sum(ProductionRequirement.production_demand_quantity),
        )
        .join(Product, Product.id == ProductionRequirement.product_id)
        .where(
            ProductionRequirement.business_id == business.id,
            Product.business_id == business.id,
            ProductionRequirement.calculation_status == CalculationStatus.INCOMPLETE_RECIPE,
        )
        .group_by(ProductionRequirement.product_id, Product.name)
    ).all()
    for product_id, product_name, first_date, total_qty in incomplete_rows:
        findings.append(
            AlertFinding(
                AlertCode.PRODUCT_MISSING_RECIPE,
                str(product_id),
                product_name,
                f"{_fmt(Decimal(total_qty))} units of demand from "
                f"{first_date.isoformat()} cannot be planned",
                f"/app/products/{product_id}",
            )
        )

    # ------------------------------------------------------------ Active runs
    run_rows = db.execute(
        select(ProductionRun, Product.name)
        .join(Product, Product.id == ProductionRun.product_id)
        .where(
            ProductionRun.business_id == business.id,
            Product.business_id == business.id,
            ProductionRun.status == ProductionRunStatus.IN_PRODUCTION,
        )
        .order_by(ProductionRun.started_at.asc())
    ).all()
    active_runs: list[ActiveRunResponse] = []
    for run, product_name in run_rows:
        progress = alerts_domain.calculate_run_progress(
            started_at=run.started_at,
            now_utc=now_utc,
            planned_batches=run.planned_batches,
            elapsed_minutes_per_batch=run.elapsed_minutes_per_batch_snapshot,
        )
        active_runs.append(
            ActiveRunResponse(
                run_id=str(run.id),
                product_id=str(run.product_id),
                product_name=product_name,
                planned_batches=run.planned_batches,
                planned_output_quantity=run.planned_output_quantity,
                started_at=run.started_at,
                elapsed_minutes=progress.elapsed_minutes,
                expected_elapsed_minutes=progress.expected_elapsed_minutes,
                expected_finish_at=progress.expected_finish_at,
                is_past_expected=progress.is_past_expected,
            )
        )

    # ------------------------------------------------------- Shopping shortages
    shopping = shopping_list_service.get_shopping_list(db, business, business_today=business_today)
    for item in shopping.shortages:
        findings.append(
            AlertFinding(
                AlertCode.INGREDIENT_SHORTAGE,
                str(item.ingredient_id),
                item.ingredient_name,
                f"short {_fmt(item.shortage_quantity)} {item.canonical_unit} "
                f"(needed by {item.needed_by_date.isoformat() if item.needed_by_date else '—'})",
                "/app/production/shopping-list",
            )
        )

    # ------------------------------------------------- Purchased-product shortage
    purchased_product_ids = list(
        db.scalars(
            select(PurchasedProductReservation.product_id)
            .where(PurchasedProductReservation.business_id == business.id)
            .distinct()
        )
    )
    if purchased_product_ids:
        for product in db.scalars(
            select(Product).where(
                Product.id.in_(purchased_product_ids), Product.business_id == business.id
            )
        ):
            shortage = readiness_service.purchased_shortage_for_product(db, business, product)
            if shortage > 0:
                findings.append(
                    AlertFinding(
                        AlertCode.PURCHASED_PRODUCT_SHORTAGE,
                        str(product.id),
                        product.name,
                        f"short {_fmt(shortage)} units",
                        f"/app/inventory/purchased-products/{product.id}/restock",
                    )
                )

    # ------------------------------------------------- Negative ingredient balance
    for ingredient in db.scalars(
        select(Ingredient).where(
            Ingredient.business_id == business.id, Ingredient.physical_quantity < 0
        )
    ):
        findings.append(
            AlertFinding(
                AlertCode.NEGATIVE_INGREDIENT_INVENTORY,
                str(ingredient.id),
                ingredient.name,
                f"recorded {_fmt(ingredient.physical_quantity)} {ingredient.canonical_unit}",
                f"/app/inventory/ingredients/{ingredient.id}",
            )
        )

    # ----------------------------------------------------------- Expiring Surplus
    window_end = business_today + timedelta(days=alerts_domain.EXPIRING_SURPLUS_WINDOW_DAYS)
    candidate_lots = list(
        db.scalars(
            select(SurplusInventory)
            .options(selectinload(SurplusInventory.product))
            .where(
                SurplusInventory.business_id == business.id,
                SurplusInventory.is_reusable.is_(True),
                SurplusInventory.physical_quantity > 0,
                SurplusInventory.usable_through_date.is_not(None),
                SurplusInventory.usable_through_date >= business_today,
                SurplusInventory.usable_through_date <= window_end,
            )
        )
    )
    allocated = availability_service.surplus_allocated_quantities(
        db, business, [lot.id for lot in candidate_lots]
    )
    for lot in candidate_lots:
        available = calculate_available(lot.physical_quantity, allocated[lot.id])
        if alerts_domain.is_surplus_expiring(
            is_reusable=lot.is_reusable,
            available_quantity=available,
            usable_through_date=lot.usable_through_date,
            business_today=business_today,
        ):
            findings.append(
                AlertFinding(
                    AlertCode.SURPLUS_EXPIRING,
                    str(lot.id),
                    lot.product.name,
                    f"{_fmt(available)} available · usable through "
                    f"{lot.usable_through_date.isoformat()}",
                    "/app/surplus",
                )
            )

    # ----------------------------------------------- Today's Fulfillments section
    todays_orders = sorted(
        (o for o in active_orders if o.fulfillment_date == business_today), key=_time_sort_key
    )
    payments_total: dict[uuid.UUID, Decimal] = {}
    lines_by_order: dict[uuid.UUID, Order] = {}
    if todays_orders:
        ids = [o.id for o in todays_orders]
        payments_total = {
            order_id: Decimal(total)
            for order_id, total in db.execute(
                select(Payment.order_id, func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.business_id == business.id, Payment.order_id.in_(ids))
                .group_by(Payment.order_id)
            )
        }
        lines_by_order = {
            o.id: o
            for o in db.scalars(
                select(Order)
                .options(selectinload(Order.lines))
                .where(Order.business_id == business.id, Order.id.in_(ids))
            )
        }

    fulfillments: list[FulfillmentItemResponse] = []
    for order in todays_orders:
        paid = order_pricing.quantize_money(payments_total.get(order.id, Decimal(0)))
        unsatisfied: list[UnsatisfiedLineResponse] = []
        if order.status is OrderStatus.CONFIRMED:
            unsatisfied = [
                UnsatisfiedLineResponse(
                    line_id=str(u.line_id), display_name=u.display_name, reason=u.reason
                )
                for u in readiness_service.unsatisfied_lines(db, business, lines_by_order[order.id])
            ]
        fulfillments.append(
            FulfillmentItemResponse(
                order_id=str(order.id),
                order_number=order.order_number,
                customer_name=order.customer_name,
                status=order.status.value,
                fulfillment_time=order.fulfillment_time,
                fulfillment_method=order.fulfillment_method,
                final_total=order.final_total,
                payment_status=order_pricing.derive_payment_status(paid, order.final_total).value,
                timing=timing_by_order[order.id].value,
                unsatisfied_lines=unsatisfied,
            )
        )

    # ------------------------------------------------ Custom Item manual workload
    manual_workload: ManualWorkloadResponse | None = None
    workload_rows = db.execute(
        select(OrderLine.order_id, OrderLine.custom_active_time_minutes)
        .join(Order, Order.id == OrderLine.order_id)
        .where(
            Order.business_id == business.id,
            OrderLine.business_id == business.id,
            Order.status == OrderStatus.CONFIRMED,
            Order.fulfillment_date == business_today,
            OrderLine.line_type == OrderLineType.CUSTOM_ITEM,
            OrderLine.custom_active_time_minutes.is_not(None),
            OrderLine.custom_active_time_minutes > 0,
            OrderLine.manual_fulfillment_satisfied.is_(False),
        )
    ).all()
    if workload_rows:
        manual_workload = ManualWorkloadResponse(
            total_minutes=sum(minutes for _order_id, minutes in workload_rows),
            line_count=len(workload_rows),
            order_count=len({order_id for order_id, _minutes in workload_rows}),
        )

    # -------------------------------------------------------------- Snapshot
    due_today = [o for o in active_orders if o.fulfillment_date == business_today]
    snapshot = SnapshotResponse(
        orders_due_today=len(due_today),
        orders_ready=sum(1 for o in due_today if o.status is OrderStatus.READY),
        orders_awaiting=sum(1 for o in due_today if o.status is OrderStatus.CONFIRMED),
        orders_at_risk=sum(
            1 for o in due_today if timing_by_order[o.id] is FulfillmentTiming.AT_RISK
        ),
        orders_overdue=sum(
            1 for o in active_orders if timing_by_order[o.id] is FulfillmentTiming.OVERDUE
        ),
        scheduled_order_value_today=order_pricing.quantize_money(
            sum((o.final_total for o in due_today), Decimal(0))
        ),
        production_items_today=len(production_items),
        planned_batches_today=sum(i.recommended_batches or 0 for i in production_items),
        active_runs=len(active_runs),
    )

    # --------------------------------------------------------------- Upcoming
    upcoming: list[UpcomingDayResponse] = []
    for offset in range(1, UPCOMING_DAYS + 1):
        day = business_today + timedelta(days=offset)
        day_orders = sorted(
            (o for o in active_orders if o.fulfillment_date == day), key=_time_sort_key
        )
        day_requirements = upcoming_production.get(day, [])
        if not day_orders and not day_requirements:
            continue
        upcoming.append(
            UpcomingDayResponse(
                date=day,
                order_count=len(day_orders),
                scheduled_order_value=order_pricing.quantize_money(
                    sum((o.final_total for o in day_orders), Decimal(0))
                ),
                orders=[
                    UpcomingOrderResponse(
                        order_id=str(o.id),
                        order_number=o.order_number,
                        customer_name=o.customer_name,
                        status=o.status.value,
                        fulfillment_time=o.fulfillment_time,
                    )
                    for o in day_orders[:UPCOMING_ORDERS_PER_DAY_CAP]
                ],
                orders_truncated=len(day_orders) > UPCOMING_ORDERS_PER_DAY_CAP,
                production_item_count=len(day_requirements),
                planned_batches=sum(r.recommended_batches or 0 for r in day_requirements),
            )
        )

    attention = [
        DashboardAlertResponse(
            code=alert.code.value,
            severity=alert.severity.value,
            title=alert.title,
            condition=alert.condition,
            impact=alert.impact,
            count=alert.count,
            destination_label=alert.destination_label,
            destination_path=alert.destination_path,
            items=[
                DashboardAlertItemResponse(label=i.label, detail=i.detail, path=i.path)
                for i in alert.items
            ],
            items_truncated=alert.items_truncated,
        )
        for alert in alerts_domain.aggregate_alerts(findings)
    ]

    return DashboardResponse(
        generated_at=now_utc,
        business_today=business_today,
        timezone=business.timezone,
        attention=attention,
        active_production=active_runs,
        todays_production=TodaysProductionResponse(
            items=production_items, manual_workload=manual_workload
        ),
        todays_fulfillments=fulfillments,
        todays_snapshot=snapshot,
        upcoming=upcoming,
    )
