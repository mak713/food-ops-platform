"""Historical Analytics read model (Spec §6.14, §11.11, §13; Phase 10).

Strictly read-side (extends ADR-144): no `FOR UPDATE`, no writes, no cache, no persisted
reporting state, default READ COMMITTED, and **no snapshot guarantee** across calls. To keep a
single *response* internally coherent each report section is produced by **one SQL
statement** (Overview: both periods in one scan; Trends; Products: rows + breakdown +
reconciliation via `UNION ALL`; Customers and Orders: summary + page via a single-row summary
CTE left-joined to the page).

PostgreSQL does all filtering, joining, grouping and summing. Python only shapes the result:
Decimal KPI derivation (`app.domain.analytics_kpis`), zero-filling the (Python-generated)
Business-local buckets, small deterministic sorts of catalog-bounded product rows, and
row-by-row CSV serialization. Python never iterates Orders/Lines/ledger rows to total them.

Tenant scoping: every table in every query graph carries its own `business_id` predicate —
a joined row is never trusted merely because its relationship points at a tenant row.

Period membership always uses `Order.completed_at` (Completed) or `Order.canceled_at`
(Canceled) against the half-open UTC interval produced by `app.domain.analytics_periods`.
Historical cost is selected *through the Order* — never by `OrderCostAllocation.created_at`.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    UUID,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    and_,
    case,
    cast,
    column,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.db.columns import MONEY
from app.db.enums import FulfillmentMethod, OrderLineType, OrderStatus
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation as OCA
from app.db.models.customer import Customer
from app.db.models.order import Order, OrderLine, Payment
from app.db.models.product import Product, SellingOption
from app.domain import analytics_kpis as kpi
from app.domain.analytics_periods import (
    Bucket,
    Granularity,
    Period,
    ResolvedPeriods,
    build_buckets,
)
from app.domain.order_pricing import PaymentStatus, derive_payment_status

ZERO_MONEY = Decimal("0.00")
UNAVAILABLE_CUSTOMER = "(unavailable customer)"
UNAVAILABLE_PRODUCT = "(unavailable product)"
UNAVAILABLE_SELLING_OPTION = "(unavailable selling option)"
STREAM_CHUNK = 500


class SortDirection(enum.StrEnum):
    ASC = "asc"
    DESC = "desc"


# ======================================================================================
# Shared query fragments
# ======================================================================================


def _completed_in(business_id: uuid.UUID, lower: datetime, upper: datetime) -> list:
    """Completed Orders whose `completed_at` belongs to `[lower, upper)` (tenant-scoped)."""
    return [
        Order.business_id == business_id,
        Order.status == OrderStatus.COMPLETED,
        Order.completed_at >= lower,
        Order.completed_at < upper,
    ]


def _money(value: Decimal | int | None) -> Decimal:
    return kpi.quantize_money(Decimal(0) if value is None else Decimal(value))


def _to_kpis(revenue, cost, orders) -> kpi.Kpis:
    return kpi.compute_kpis(
        revenue=Decimal(revenue), direct_cost=Decimal(cost), completed_orders=int(orders)
    )


# ======================================================================================
# Overview
# ======================================================================================


@dataclass(frozen=True)
class OverviewReport:
    current: kpi.Kpis
    previous: kpi.Kpis | None
    deltas: kpi.KpiDeltas | None
    previous_has_data: bool | None


def get_overview(db: Session, business: Business, periods: ResolvedPeriods) -> OverviewReport:
    """Both periods from one scan of `[prev.lower, cur.upper)` (they are contiguous) using
    conditional aggregation. Ledger rows are summed *per Order first* and then joined 1:1
    to the Order, so `final_total` is never multiplied by ledger fan-out."""
    cur = periods.current
    prev = periods.previous
    scan_lower = prev.lower_bound if prev is not None else cur.lower_bound

    po = (
        select(Order.id.label("id"), Order.final_total, Order.completed_at)
        .where(*_completed_in(business.id, scan_lower, cur.upper_bound))
        .cte("po")
    )
    cost = (
        select(OCA.order_id.label("order_id"), func.sum(OCA.amount).label("cost"))
        .where(OCA.business_id == business.id, OCA.order_id.in_(select(po.c.id)))
        .group_by(OCA.order_id)
        .cte("cost")
    )

    def slice_aggs(period: Period):
        in_period = and_(
            po.c.completed_at >= period.lower_bound, po.c.completed_at < period.upper_bound
        )
        return (
            func.coalesce(func.sum(po.c.final_total).filter(in_period), 0),
            func.coalesce(func.sum(cost.c.cost).filter(in_period), 0),
            func.count().filter(in_period),
        )

    columns = list(slice_aggs(cur))
    if prev is not None:
        columns += list(slice_aggs(prev))
    row = db.execute(
        select(*columns).select_from(po.outerjoin(cost, cost.c.order_id == po.c.id))
    ).one()

    current = _to_kpis(row[0], row[1], row[2])
    if prev is None:
        return OverviewReport(current, None, None, None)
    previous = _to_kpis(row[3], row[4], row[5])
    return OverviewReport(
        current=current,
        previous=previous,
        deltas=kpi.compare_kpis(current, previous),
        previous_has_data=previous.completed_orders > 0,
    )


# ======================================================================================
# Trends
# ======================================================================================


@dataclass(frozen=True)
class TrendBucketRow:
    bucket: Bucket
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    completed_orders: int


@dataclass(frozen=True)
class TrendsReport:
    granularity: Granularity
    buckets: list[TrendBucketRow]
    totals: kpi.Kpis


def get_trends(db: Session, business: Business, period: Period, tz) -> TrendsReport:
    """Bucket boundaries are generated in Python (single timezone database, so period and
    bucket membership can never disagree) and passed as two arrays; PostgreSQL assigns each
    Completed Order to the bucket whose half-open UTC interval contains `completed_at`."""
    granularity, buckets = build_buckets(period, tz)

    ts_array = ARRAY(DateTime(timezone=True))
    bk = (
        func.unnest(
            literal([b.lower_bound for b in buckets], ts_array),
            literal([b.upper_bound for b in buckets], ts_array),
        )
        .table_valued(
            column("lo", DateTime(timezone=True)),
            column("hi", DateTime(timezone=True)),
            with_ordinality="ord",
        )
        .render_derived(name="bk", with_types=False)
    )

    po = (
        select(Order.id.label("id"), Order.final_total, Order.completed_at)
        .where(*_completed_in(business.id, period.lower_bound, period.upper_bound))
        .cte("po")
    )
    cost = (
        select(OCA.order_id.label("order_id"), func.sum(OCA.amount).label("cost"))
        .where(OCA.business_id == business.id, OCA.order_id.in_(select(po.c.id)))
        .group_by(OCA.order_id)
        .cte("cost")
    )
    stmt = (
        select(
            bk.c.ord,
            func.coalesce(func.sum(po.c.final_total), 0),
            func.coalesce(func.sum(cost.c.cost), 0),
            func.count(po.c.id),
        )
        .select_from(
            bk.outerjoin(
                po, and_(po.c.completed_at >= bk.c.lo, po.c.completed_at < bk.c.hi)
            ).outerjoin(cost, cost.c.order_id == po.c.id)
        )
        .group_by(bk.c.ord)
        .order_by(bk.c.ord)
    )
    by_index = {int(r[0]) - 1: r for r in db.execute(stmt)}

    rows: list[TrendBucketRow] = []
    total_revenue = Decimal(0)
    total_cost = Decimal(0)
    total_orders = 0
    for bucket in buckets:
        r = by_index.get(bucket.index)
        revenue = _money(r[1]) if r else ZERO_MONEY
        direct_cost = _money(r[2]) if r else ZERO_MONEY
        orders = int(r[3]) if r else 0
        rows.append(
            TrendBucketRow(
                bucket=bucket,
                revenue=revenue,
                direct_cost=direct_cost,
                contribution=kpi.contribution(revenue, direct_cost),
                completed_orders=orders,
            )
        )
        total_revenue += revenue
        total_cost += direct_cost
        total_orders += orders
    totals = kpi.compute_kpis(
        revenue=total_revenue, direct_cost=total_cost, completed_orders=total_orders
    )
    return TrendsReport(granularity=granularity, buckets=rows, totals=totals)


# ======================================================================================
# Products
# ======================================================================================


class ProductSort(enum.StrEnum):
    CONTRIBUTION = "contribution"
    MARGIN = "margin"
    REVENUE = "revenue"
    UNITS = "units"
    ORDERS = "orders"
    NAME = "name"


@dataclass(frozen=True)
class ProductOptionRow:
    kind: OrderLineType  # STANDARD_OPTION | CUSTOM_QUANTITY
    selling_option_id: uuid.UUID | None
    name: str
    package_count: Decimal | None
    underlying_units: Decimal
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None
    completed_orders: int


@dataclass(frozen=True)
class ProductRow:
    product_id: uuid.UUID
    name: str
    product_type: str | None
    is_active: bool | None
    underlying_units: Decimal
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None
    completed_orders: int
    selling_options: list[ProductOptionRow] = field(default_factory=list)


@dataclass(frozen=True)
class CustomItemsRow:
    line_count: int
    completed_orders: int
    revenue: Decimal
    direct_cost: Decimal
    contribution: Decimal
    contribution_margin: Decimal | None


@dataclass(frozen=True)
class OrderLevelReconciliation:
    order_adjustments: Decimal
    manual_tax: Decimal
    unattributed_direct_cost: Decimal
    unreconciled_revenue: Decimal


@dataclass(frozen=True)
class ProductHighlight:
    product_id: uuid.UUID
    name: str
    value: Decimal


@dataclass(frozen=True)
class ProductsReport:
    rows: list[ProductRow]
    custom_items: CustomItemsRow | None
    order_level: OrderLevelReconciliation
    totals: kpi.Kpis
    most_profitable: ProductHighlight | None
    highest_margin: ProductHighlight | None


def _nul(sql_type):
    return cast(null(), sql_type)


def get_products(
    db: Session,
    business: Business,
    period: Period,
    *,
    sort: ProductSort = ProductSort.CONTRIBUTION,
    direction: SortDirection | None = None,
) -> ProductsReport:
    b = business.id
    po = (
        select(
            Order.id.label("id"),
            Order.final_total,
            Order.order_adjustment,
            Order.manual_tax,
        )
        .where(*_completed_in(b, period.lower_bound, period.upper_bound))
        .cte("po")
    )

    # Ledger rows of the period's Orders, tagged by whether their `order_line_id` resolves to
    # a line of that same Order. Unmatched rows (NULL or inconsistent line) are never
    # attributed to a Product — they are surfaced as `unattributed_direct_cost`.
    led = (
        select(
            OCA.order_id.label("order_id"),
            OCA.order_line_id.label("order_line_id"),
            OCA.amount.label("amount"),
            (OrderLine.id.is_not(None)).label("matched"),
        )
        .select_from(
            OCA.__table__.join(po, po.c.id == OCA.order_id).outerjoin(
                OrderLine.__table__,
                and_(
                    OrderLine.id == OCA.order_line_id,
                    OrderLine.order_id == OCA.order_id,
                    OrderLine.business_id == b,
                ),
            )
        )
        .where(OCA.business_id == b)
        .cte("led")
    )
    lc = (
        select(led.c.order_line_id, func.sum(led.c.amount).label("cost"))
        .where(led.c.matched)
        .group_by(led.c.order_line_id)
        .cte("lc")
    )
    lines = (
        select(
            OrderLine.order_id.label("order_id"),
            OrderLine.product_id.label("product_id"),
            OrderLine.selling_option_id.label("selling_option_id"),
            OrderLine.line_type.label("line_type"),
            OrderLine.package_quantity.label("package_quantity"),
            OrderLine.underlying_quantity.label("underlying_quantity"),
            OrderLine.line_subtotal.label("line_subtotal"),
            func.coalesce(lc.c.cost, 0).label("cost"),
            # Tenant-local resolution of the line's references. `p_id` is non-NULL only for a
            # Product owned by THIS Business; `so_id` only for a Selling Option owned by this
            # Business that belongs to that same resolved Product. Raw reference ids that fail
            # to resolve are never selected into any output.
            Product.id.label("p_id"),
            SellingOption.id.label("so_id"),
        )
        .select_from(
            OrderLine.__table__.join(po, po.c.id == OrderLine.order_id)
            .outerjoin(lc, lc.c.order_line_id == OrderLine.id)
            .outerjoin(
                Product.__table__,
                and_(Product.id == OrderLine.product_id, Product.business_id == b),
            )
            .outerjoin(
                SellingOption.__table__,
                and_(
                    SellingOption.id == OrderLine.selling_option_id,
                    SellingOption.business_id == b,
                    SellingOption.product_id == Product.id,
                ),
            )
        )
        .where(OrderLine.business_id == b)
        .cte("lines")
    )

    def sel(
        kind: str,
        *,
        product_id=None,
        option_id=None,
        line_type=None,
        packages=None,
        units=None,
        revenue=None,
        cost=None,
        orders=None,
        line_count=None,
        **kw,
    ):
        cols = [
            literal(kind).label("kind"),
            (product_id if product_id is not None else _nul(UUID(as_uuid=True))).label(
                "product_id"
            ),
            (option_id if option_id is not None else _nul(UUID(as_uuid=True))).label(
                "selling_option_id"
            ),
            (line_type if line_type is not None else _nul(String)).label("line_type"),
            (packages if packages is not None else _nul(Numeric)).label("packages"),
            (units if units is not None else _nul(Numeric)).label("units"),
            (revenue if revenue is not None else _nul(Numeric)).label("revenue"),
            (cost if cost is not None else _nul(Numeric)).label("cost"),
            (orders if orders is not None else _nul(Integer)).label("orders"),
            (line_count if line_count is not None else _nul(Integer)).label("line_count"),
        ]
        return select(*cols, **kw)

    not_custom_item = lines.c.line_type != OrderLineType.CUSTOM_ITEM
    # A Product-backed line is a Product-analytics fact only if its Product resolves to this
    # Business; otherwise it is quarantined (below) — never attributed, never exposed.
    resolved_product_line = and_(not_custom_item, lines.c.p_id.is_not(None))
    quarantined_product_line = and_(not_custom_item, lines.c.p_id.is_(None))
    product_level = (
        sel(
            "PRODUCT",
            product_id=lines.c.product_id,
            units=func.sum(lines.c.underlying_quantity),
            revenue=func.sum(lines.c.line_subtotal),
            cost=func.sum(lines.c.cost),
            orders=func.count(func.distinct(lines.c.order_id)),
        )
        .where(resolved_product_line)
        .group_by(lines.c.product_id)
    )

    option_level = (
        sel(
            "OPTION",
            product_id=lines.c.product_id,
            option_id=lines.c.so_id,
            line_type=cast(lines.c.line_type, String),
            packages=case(
                (
                    lines.c.line_type == OrderLineType.STANDARD_OPTION,
                    func.sum(lines.c.package_quantity),
                ),
                else_=_nul(Numeric),
            ),
            units=func.sum(lines.c.underlying_quantity),
            revenue=func.sum(lines.c.line_subtotal),
            cost=func.sum(lines.c.cost),
            orders=func.count(func.distinct(lines.c.order_id)),
        )
        .where(resolved_product_line)
        .group_by(lines.c.product_id, lines.c.line_type, lines.c.so_id)
    )

    custom_items = sel(
        "CUSTOM_ITEMS",
        revenue=func.coalesce(func.sum(lines.c.line_subtotal), 0),
        cost=func.coalesce(func.sum(lines.c.cost), 0),
        orders=func.count(func.distinct(lines.c.order_id)),
        line_count=func.count(),
    ).where(lines.c.line_type == OrderLineType.CUSTOM_ITEM)

    adjustments = sel("ORDER_ADJUSTMENT", revenue=func.coalesce(func.sum(po.c.order_adjustment), 0))
    tax = sel("MANUAL_TAX", revenue=func.coalesce(func.sum(po.c.manual_tax), 0))
    unattributed = sel(
        "UNATTRIBUTED_COST",
        cost=func.coalesce(func.sum(led.c.amount), 0),
    ).where(led.c.matched.is_(False))
    # Line-attributed cost of lines whose Product does not resolve tenant-locally. Folded into
    # `unattributed_direct_cost`; their Revenue stays visible as `unreconciled_revenue`.
    quarantined = sel("QUARANTINED_COST", cost=func.coalesce(func.sum(lines.c.cost), 0)).where(
        quarantined_product_line
    )
    totals = sel(
        "TOTALS",
        revenue=func.coalesce(func.sum(po.c.final_total), 0),
        cost=select(func.coalesce(func.sum(led.c.amount), 0)).scalar_subquery(),
        orders=func.count(),
    ).select_from(po)

    # `FROM` clauses: the adjustment/tax/totals selects read `po`; unattributed reads `led`.
    adjustments = adjustments.select_from(po)
    tax = tax.select_from(po)
    unattributed = unattributed.select_from(led)
    quarantined = quarantined.select_from(lines)

    union = union_all(
        product_level,
        option_level,
        custom_items,
        adjustments,
        tax,
        unattributed,
        quarantined,
        totals,
    )
    raw = db.execute(union).all()

    product_aggs: dict[uuid.UUID, dict] = {}
    option_aggs: dict[uuid.UUID, list[dict]] = {}
    custom_agg: dict | None = None
    scalar: dict[str, tuple] = {}
    for r in raw:
        kind = r.kind
        if kind == "PRODUCT":
            product_aggs[r.product_id] = {
                "units": Decimal(r.units),
                "revenue": _money(r.revenue),
                "cost": _money(r.cost),
                "orders": int(r.orders),
            }
        elif kind == "OPTION":
            option_aggs.setdefault(r.product_id, []).append(
                {
                    "option_id": r.selling_option_id,
                    "line_type": OrderLineType(r.line_type),
                    "packages": None if r.packages is None else Decimal(r.packages),
                    "units": Decimal(r.units),
                    "revenue": _money(r.revenue),
                    "cost": _money(r.cost),
                    "orders": int(r.orders),
                }
            )
        elif kind == "CUSTOM_ITEMS":
            if int(r.line_count) > 0:
                custom_agg = {
                    "lines": int(r.line_count),
                    "orders": int(r.orders),
                    "revenue": _money(r.revenue),
                    "cost": _money(r.cost),
                }
        else:
            scalar[kind] = (r.revenue, r.cost, r.orders)

    # Labels only (current names). Numbers never come from these rows.
    product_labels: dict[uuid.UUID, tuple[str, str, bool]] = {}
    if product_aggs:
        for p in db.execute(
            select(Product.id, Product.name, Product.product_type, Product.is_active).where(
                Product.business_id == b, Product.id.in_(list(product_aggs))
            )
        ):
            product_labels[p.id] = (p.name, str(p.product_type.value), p.is_active)
    option_ids = {a["option_id"] for aggs in option_aggs.values() for a in aggs if a["option_id"]}
    option_labels: dict[uuid.UUID, str] = {}
    if option_ids:
        for o in db.execute(
            select(SellingOption.id, SellingOption.name).where(
                SellingOption.business_id == b, SellingOption.id.in_(list(option_ids))
            )
        ):
            option_labels[o.id] = o.name

    rows: list[ProductRow] = []
    for pid, agg in product_aggs.items():
        name, ptype, active = product_labels.get(pid, (UNAVAILABLE_PRODUCT, None, None))
        options: list[ProductOptionRow] = []
        for oa in option_aggs.get(pid, []):
            contribution_value = kpi.contribution(oa["revenue"], oa["cost"])
            if oa["line_type"] is OrderLineType.STANDARD_OPTION:
                label = option_labels.get(oa["option_id"], UNAVAILABLE_SELLING_OPTION)
            else:
                label = "Custom quantity"
            options.append(
                ProductOptionRow(
                    kind=oa["line_type"],
                    selling_option_id=oa["option_id"],
                    name=label,
                    package_count=oa["packages"],
                    underlying_units=oa["units"],
                    revenue=oa["revenue"],
                    direct_cost=oa["cost"],
                    contribution=contribution_value,
                    contribution_margin=kpi.contribution_margin(contribution_value, oa["revenue"]),
                    completed_orders=oa["orders"],
                )
            )
        options.sort(
            key=lambda o: (
                o.kind is OrderLineType.CUSTOM_QUANTITY,
                -o.contribution,
                o.name.casefold(),
                str(o.selling_option_id),
            )
        )
        contribution_value = kpi.contribution(agg["revenue"], agg["cost"])
        rows.append(
            ProductRow(
                product_id=pid,
                name=name,
                product_type=ptype,
                is_active=active,
                underlying_units=agg["units"],
                revenue=agg["revenue"],
                direct_cost=agg["cost"],
                contribution=contribution_value,
                contribution_margin=kpi.contribution_margin(contribution_value, agg["revenue"]),
                completed_orders=agg["orders"],
                selling_options=options,
            )
        )

    rows = _sort_products(rows, sort, direction)

    custom_row: CustomItemsRow | None = None
    if custom_agg is not None:
        c = kpi.contribution(custom_agg["revenue"], custom_agg["cost"])
        custom_row = CustomItemsRow(
            line_count=custom_agg["lines"],
            completed_orders=custom_agg["orders"],
            revenue=custom_agg["revenue"],
            direct_cost=custom_agg["cost"],
            contribution=c,
            contribution_margin=kpi.contribution_margin(c, custom_agg["revenue"]),
        )

    total_revenue = _money(scalar["TOTALS"][0])
    total_cost = _money(scalar["TOTALS"][1])
    adjustments_total = _money(scalar["ORDER_ADJUSTMENT"][0])
    tax_total = _money(scalar["MANUAL_TAX"][0])
    unattributed_cost = _money(scalar["UNATTRIBUTED_COST"][1]) + _money(
        scalar["QUARANTINED_COST"][1]
    )
    attributed_revenue = sum((r.revenue for r in rows), ZERO_MONEY) + (
        custom_row.revenue if custom_row else ZERO_MONEY
    )
    order_level = OrderLevelReconciliation(
        order_adjustments=adjustments_total,
        manual_tax=tax_total,
        unattributed_direct_cost=unattributed_cost,
        unreconciled_revenue=_money(
            total_revenue - attributed_revenue - adjustments_total - tax_total
        ),
    )
    totals_kpis = kpi.compute_kpis(
        revenue=total_revenue,
        direct_cost=total_cost,
        completed_orders=int(scalar["TOTALS"][2]),
    )

    return ProductsReport(
        rows=rows,
        custom_items=custom_row,
        order_level=order_level,
        totals=totals_kpis,
        most_profitable=_most_profitable(rows),
        highest_margin=_highest_margin(rows),
    )


def _sort_products(
    rows: list[ProductRow], sort: ProductSort, direction: SortDirection | None
) -> list[ProductRow]:
    """Small deterministic sort of a catalog-bounded list (not a bulk calculation).
    Total order: primary key (+direction), then Contribution desc, name asc, id asc.
    Margin sorting always places an undefined margin (zero Revenue) last."""
    default_desc = sort is not ProductSort.NAME
    descending = (direction is SortDirection.DESC) if direction else default_desc

    ordered = sorted(rows, key=lambda r: str(r.product_id))
    ordered.sort(key=lambda r: r.name.casefold())
    ordered.sort(key=lambda r: r.contribution, reverse=True)

    primary = {
        ProductSort.CONTRIBUTION: lambda r: r.contribution,
        ProductSort.REVENUE: lambda r: r.revenue,
        ProductSort.UNITS: lambda r: r.underlying_units,
        ProductSort.ORDERS: lambda r: r.completed_orders,
        ProductSort.NAME: lambda r: r.name.casefold(),
        ProductSort.MARGIN: lambda r: r.contribution_margin,
    }[sort]
    if sort is ProductSort.MARGIN:
        defined = [r for r in ordered if r.contribution_margin is not None]
        undefined = [r for r in ordered if r.contribution_margin is None]
        defined.sort(key=primary, reverse=descending)
        return defined + undefined
    ordered.sort(key=primary, reverse=descending)
    return ordered


def _most_profitable(rows: list[ProductRow]) -> ProductHighlight | None:
    """Highest total Contribution (not Margin). Ties: name asc, id asc."""
    if not rows:
        return None
    best = min(rows, key=lambda r: (-r.contribution, r.name.casefold(), str(r.product_id)))
    return ProductHighlight(best.product_id, best.name, best.contribution)


def _highest_margin(rows: list[ProductRow]) -> ProductHighlight | None:
    """Highest Contribution Margin among Products with Revenue. Ties: Contribution desc,
    name asc, id asc. Labeled distinctly from 'most profitable'."""
    candidates = [r for r in rows if r.contribution_margin is not None]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda r: (
            -r.contribution_margin,  # type: ignore[operator]
            -r.contribution,
            r.name.casefold(),
            str(r.product_id),
        ),
    )
    assert best.contribution_margin is not None
    return ProductHighlight(best.product_id, best.name, best.contribution_margin)


# ======================================================================================
# Customers
# ======================================================================================


class CustomerSort(enum.StrEnum):
    REVENUE = "revenue"
    COMPLETED_ORDERS = "completed_orders"
    NAME = "name"


class CustomerClass(enum.StrEnum):
    NEW = "NEW"
    RETURNING = "RETURNING"


@dataclass(frozen=True)
class CustomerRow:
    customer_id: uuid.UUID
    name: str
    is_active: bool | None
    classification: CustomerClass
    completed_orders: int
    revenue: Decimal
    average_order_value: Decimal | None


@dataclass(frozen=True)
class GuestSummary:
    completed_orders: int
    revenue: Decimal
    average_order_value: Decimal | None
    share_of_completed_orders: Decimal | None


@dataclass(frozen=True)
class CustomersSummary:
    unique_customers: int
    new_customers: int
    returning_customers: int
    guest: GuestSummary


@dataclass(frozen=True)
class CustomersReport:
    summary: CustomersSummary
    items: list[CustomerRow]
    total: int
    limit: int
    offset: int


def _customer_cte(business_id: uuid.UUID, period: Period):
    """Per-Customer aggregates over COMPLETED Orders. A non-null `Order.customer_id` counts as
    a Customer **only if it resolves to a `Customer` row owned by this Business** (the join
    carries its own tenant predicate). An unresolved/foreign reference is neither a Customer
    nor a Guest (Guest is strictly `customer_id IS NULL`); it is simply absent from every
    customer figure and its identifier is never exposed."""
    in_period = and_(
        Order.completed_at >= period.lower_bound, Order.completed_at < period.upper_bound
    )
    return (
        select(
            Order.customer_id.label("customer_id"),
            Customer.name.label("name"),
            Customer.is_active.label("is_active"),
            func.count().filter(in_period).label("n"),
            func.coalesce(func.sum(Order.final_total).filter(in_period), 0).label("revenue"),
            func.count().filter(Order.completed_at < period.lower_bound).label("n_before"),
        )
        .select_from(
            Order.__table__.join(
                Customer.__table__,
                and_(Customer.id == Order.customer_id, Customer.business_id == business_id),
            )
        )
        .where(
            Order.business_id == business_id,
            Order.status == OrderStatus.COMPLETED,
            Order.completed_at < period.upper_bound,
        )
        .group_by(Order.customer_id, Customer.name, Customer.is_active)
        .having(func.count().filter(in_period) > 0)
        .cte("cust")
    )


def _customer_order_by(sort: CustomerSort, direction: SortDirection | None, c):
    """Total order over columns `c.n`, `c.revenue`, `c.name`, `c.customer_id`."""
    default_desc = sort is not CustomerSort.NAME
    desc = (direction is SortDirection.DESC) if direction else default_desc
    lowered = func.lower(c.name)
    keys = {
        CustomerSort.REVENUE: c.revenue,
        CustomerSort.COMPLETED_ORDERS: c.n,
        CustomerSort.NAME: lowered,
    }
    primary = keys[sort]
    ordering = [primary.desc() if desc else primary.asc()]
    if sort is not CustomerSort.REVENUE:
        ordering.append(c.revenue.desc())
    if sort is not CustomerSort.COMPLETED_ORDERS:
        ordering.append(c.n.desc())
    if sort is not CustomerSort.NAME:
        ordering.append(lowered.asc())
    ordering.append(c.customer_id.asc())
    return ordering


def _customer_row(customer_id, name, is_active, n, revenue, n_before) -> CustomerRow:
    revenue_q = _money(revenue)
    return CustomerRow(
        customer_id=customer_id,
        name=name,
        is_active=is_active,
        classification=CustomerClass.NEW if int(n_before) == 0 else CustomerClass.RETURNING,
        completed_orders=int(n),
        revenue=revenue_q,
        average_order_value=kpi.average_order_value(revenue_q, int(n)),
    )


def _guest_summary(guest_orders: int, guest_revenue, completed_total: int) -> GuestSummary:
    revenue_q = _money(guest_revenue)
    share = None
    if completed_total > 0:
        share = kpi.quantize_ratio(Decimal(guest_orders) / Decimal(completed_total))
    return GuestSummary(
        completed_orders=int(guest_orders),
        revenue=revenue_q,
        average_order_value=kpi.average_order_value(revenue_q, int(guest_orders)),
        share_of_completed_orders=share,
    )


def get_customers(
    db: Session,
    business: Business,
    period: Period,
    *,
    sort: CustomerSort = CustomerSort.REVENUE,
    direction: SortDirection | None = None,
    limit: int,
    offset: int,
) -> CustomersReport:
    b = business.id
    cust = _customer_cte(b, period)
    in_period = and_(
        Order.completed_at >= period.lower_bound, Order.completed_at < period.upper_bound
    )

    summary = (
        select(
            func.count().label("unique_customers"),
            func.count().filter(cust.c.n_before == 0).label("new_customers"),
            func.count().filter(cust.c.n_before > 0).label("returning_customers"),
        )
        .select_from(cust)
        .cte("summary")
    )

    totals = (
        select(
            func.count().filter(Order.customer_id.is_(None)).label("guest_orders"),
            func.coalesce(func.sum(Order.final_total).filter(Order.customer_id.is_(None)), 0).label(
                "guest_revenue"
            ),
            func.count().label("completed_total"),
        )
        .where(Order.business_id == b, Order.status == OrderStatus.COMPLETED, in_period)
        .cte("totals")
    )

    page = (
        select(cust)
        .order_by(*_customer_order_by(sort, direction, cust.c))
        .limit(limit)
        .offset(offset)
        .subquery("page")
    )
    stmt = (
        select(
            summary.c.unique_customers,
            summary.c.new_customers,
            summary.c.returning_customers,
            totals.c.guest_orders,
            totals.c.guest_revenue,
            totals.c.completed_total,
            page.c.customer_id,
            page.c.n,
            page.c.revenue,
            page.c.n_before,
            page.c.name,
            page.c.is_active,
        )
        .select_from(summary.join(totals, true()).outerjoin(page, true()))
        .order_by(*_customer_order_by(sort, direction, page.c))
    )
    result = db.execute(stmt).all()
    head = result[0]
    items = [
        _customer_row(r.customer_id, r.name, r.is_active, r.n, r.revenue, r.n_before)
        for r in result
        if r.customer_id is not None
    ]
    return CustomersReport(
        summary=CustomersSummary(
            unique_customers=int(head.unique_customers),
            new_customers=int(head.new_customers),
            returning_customers=int(head.returning_customers),
            guest=_guest_summary(head.guest_orders, head.guest_revenue, head.completed_total),
        ),
        items=items,
        total=int(head.unique_customers),
        limit=limit,
        offset=offset,
    )


def stream_customer_export(
    db: Session, business: Business, period: Period, *, sort: CustomerSort, direction
) -> Iterator[CustomerRow | GuestSummary]:
    """The Customers CSV: every resolved Customer row (no paging, no ceiling) in the same total
    order as the JSON report, followed by exactly one Guest-summary row — all produced by **one
    SQL statement** (`UNION ALL` of the Customer rows and a single Guest/totals row, ordered by
    a group key so the Guest row is last). One statement means one snapshot: a concurrent
    Completion can never make the Guest row and the Customer rows describe different database
    states. Fetched through a server-side cursor in `STREAM_CHUNK` batches."""
    b = business.id
    cust = _customer_cte(b, period)
    in_period = and_(
        Order.completed_at >= period.lower_bound, Order.completed_at < period.upper_bound
    )
    totals = (
        select(
            func.count().filter(Order.customer_id.is_(None)).label("guest_orders"),
            func.coalesce(func.sum(Order.final_total).filter(Order.customer_id.is_(None)), 0).label(
                "guest_revenue"
            ),
            func.count().label("completed_total"),
        )
        .where(Order.business_id == b, Order.status == OrderStatus.COMPLETED, in_period)
        .cte("totals")
    )
    customer_rows = select(
        literal_column("0").label("grp"),
        cust.c.customer_id.label("customer_id"),
        cust.c.n.label("n"),
        cust.c.revenue.label("revenue"),
        cust.c.n_before.label("n_before"),
        cust.c.name.label("name"),
        cust.c.is_active.label("is_active"),
        cast(null(), BigInteger).label("completed_total"),
    )
    guest_row = select(
        literal_column("1").label("grp"),
        cast(null(), UUID(as_uuid=True)).label("customer_id"),
        totals.c.guest_orders.label("n"),
        totals.c.guest_revenue.label("revenue"),
        cast(null(), BigInteger).label("n_before"),
        cast(null(), String).label("name"),
        cast(null(), Boolean).label("is_active"),
        totals.c.completed_total.label("completed_total"),
    ).select_from(totals)
    combined = union_all(customer_rows, guest_row).subquery("export")
    stmt = select(combined).order_by(
        combined.c.grp, *_customer_order_by(sort, direction, combined.c)
    )
    for r in db.execute(stmt.execution_options(yield_per=STREAM_CHUNK)):
        if r.grp == 0:
            yield _customer_row(r.customer_id, r.name, r.is_active, r.n, r.revenue, r.n_before)
        else:
            yield _guest_summary(r.n, r.revenue, r.completed_total)


# ======================================================================================
# Orders
# ======================================================================================


class OrderStatusFilter(enum.StrEnum):
    ALL = "ALL"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class OrderSort(enum.StrEnum):
    REPORTED_AT = "reported_at"
    FINAL_TOTAL = "final_total"
    OUTSTANDING_BALANCE = "outstanding_balance"


@dataclass(frozen=True)
class OrderRow:
    order_id: uuid.UUID
    order_number: str
    status: OrderStatus
    reported_at: datetime
    customer_id: uuid.UUID | None
    customer_name: str | None
    fulfillment_method: FulfillmentMethod | None
    final_total: Decimal
    payments_total: Decimal
    payment_status: PaymentStatus | None
    outstanding_balance: Decimal | None
    direct_cost: Decimal | None
    contribution: Decimal | None


@dataclass(frozen=True)
class FulfillmentMethodRow:
    method: FulfillmentMethod | None  # None = unspecified
    completed_orders: int
    revenue: Decimal


@dataclass(frozen=True)
class LargestOrder:
    order_id: uuid.UUID
    order_number: str
    final_total: Decimal
    completed_at: datetime
    customer_name: str | None


@dataclass(frozen=True)
class OutstandingSummary:
    order_count: int
    balance: Decimal


@dataclass(frozen=True)
class OrdersSummary:
    completed_orders: int
    canceled_orders: int
    revenue: Decimal
    average_order_value: Decimal | None
    largest_completed_order: LargestOrder | None
    fulfillment_methods: list[FulfillmentMethodRow]
    outstanding: OutstandingSummary


@dataclass(frozen=True)
class OrdersReport:
    summary: OrdersSummary
    items: list[OrderRow]
    total: int
    limit: int
    offset: int


_METHODS: tuple[FulfillmentMethod | None, ...] = (
    FulfillmentMethod.PICKUP,
    FulfillmentMethod.DELIVERY,
    FulfillmentMethod.OTHER,
    None,
)


def _order_scope(business_id: uuid.UUID, period: Period):
    """CTE `ro`: one row per Order reported in the period — Completed by `completed_at`,
    Canceled by `canceled_at` — with its recorded payments total and (Completed only)
    Direct Cost. Payments and ledger rows are summed per Order *before* the 1:1 join."""
    reported_at = case(
        (Order.status == OrderStatus.COMPLETED, Order.completed_at),
        else_=Order.canceled_at,
    )
    scope = (
        select(
            Order.id.label("id"),
            Order.order_number.label("order_number"),
            Order.status.label("status"),
            reported_at.label("reported_at"),
            Order.customer_id.label("customer_id"),
            Order.fulfillment_method.label("fulfillment_method"),
            Order.final_total.label("final_total"),
        )
        .where(
            Order.business_id == business_id,
            or_(
                and_(
                    Order.status == OrderStatus.COMPLETED,
                    Order.completed_at >= period.lower_bound,
                    Order.completed_at < period.upper_bound,
                ),
                and_(
                    Order.status == OrderStatus.CANCELED,
                    Order.canceled_at >= period.lower_bound,
                    Order.canceled_at < period.upper_bound,
                ),
            ),
        )
        .cte("scope")
    )
    paid = (
        select(Payment.order_id.label("order_id"), func.sum(Payment.amount).label("paid"))
        .where(Payment.business_id == business_id, Payment.order_id.in_(select(scope.c.id)))
        .group_by(Payment.order_id)
        .cte("paid")
    )
    cost = (
        select(OCA.order_id.label("order_id"), func.sum(OCA.amount).label("cost"))
        .where(
            OCA.business_id == business_id,
            OCA.order_id.in_(select(scope.c.id).where(scope.c.status == OrderStatus.COMPLETED)),
        )
        .group_by(OCA.order_id)
        .cte("cost")
    )
    paid_amount = func.coalesce(paid.c.paid, 0)
    is_completed = scope.c.status == OrderStatus.COMPLETED
    ro = (
        select(
            scope.c.id,
            scope.c.order_number,
            scope.c.status,
            scope.c.reported_at,
            # Tenant-local resolution: a non-null `customer_id` that does not resolve to a
            # Customer of THIS Business is never exposed (id -> NULL) and is labelled safely —
            # it is not a Guest (Guest is strictly `customer_id IS NULL`).
            case(
                (Customer.id.is_not(None), scope.c.customer_id),
                else_=cast(null(), UUID(as_uuid=True)),
            ).label("customer_id"),
            scope.c.fulfillment_method,
            scope.c.final_total,
            paid_amount.label("paid"),
            case((is_completed, func.coalesce(cost.c.cost, 0)), else_=_nul(MONEY)).label("cost"),
            case(
                (is_completed, func.greatest(scope.c.final_total - paid_amount, 0)),
                else_=_nul(MONEY),
            ).label("balance"),
            case(
                (Customer.id.is_not(None), Customer.name),
                (scope.c.customer_id.is_(None), cast(null(), String)),
                else_=literal(UNAVAILABLE_CUSTOMER),
            ).label("customer_name"),
        )
        .select_from(
            scope.outerjoin(paid, paid.c.order_id == scope.c.id)
            .outerjoin(cost, cost.c.order_id == scope.c.id)
            .outerjoin(
                Customer.__table__,
                and_(Customer.id == scope.c.customer_id, Customer.business_id == business_id),
            )
        )
        .cte("ro")
    )
    return ro


def _order_filters(ro, status: OrderStatusFilter, outstanding_only: bool) -> list:
    clauses = []
    if status is OrderStatusFilter.COMPLETED:
        clauses.append(ro.c.status == OrderStatus.COMPLETED)
    elif status is OrderStatusFilter.CANCELED:
        clauses.append(ro.c.status == OrderStatus.CANCELED)
    if outstanding_only:
        clauses.append(and_(ro.c.status == OrderStatus.COMPLETED, ro.c.balance > 0))
    return clauses


def _order_order_by(sort: OrderSort, direction: SortDirection | None, c):
    desc = (direction is not SortDirection.ASC) if direction else True
    primary = {
        OrderSort.REPORTED_AT: c.reported_at,
        OrderSort.FINAL_TOTAL: c.final_total,
        OrderSort.OUTSTANDING_BALANCE: c.balance,
    }[sort]
    ordering = [primary.desc().nulls_last() if desc else primary.asc().nulls_last()]
    if sort is not OrderSort.REPORTED_AT:
        ordering.append(c.reported_at.desc())
    ordering.append(c.id.asc())
    return ordering


def _order_row(r) -> OrderRow:
    final_total = _money(r.final_total)
    paid = _money(r.paid)
    completed = r.status == OrderStatus.COMPLETED
    status = OrderStatus(r.status)
    direct_cost = _money(r.cost) if completed else None
    return OrderRow(
        order_id=r.id,
        order_number=r.order_number,
        status=status,
        reported_at=r.reported_at,
        customer_id=r.customer_id,
        customer_name=r.customer_name,
        fulfillment_method=FulfillmentMethod(r.fulfillment_method)
        if r.fulfillment_method
        else None,
        final_total=final_total,
        payments_total=paid,
        payment_status=derive_payment_status(paid, final_total) if completed else None,
        outstanding_balance=_money(r.balance) if completed else None,
        direct_cost=direct_cost,
        contribution=kpi.contribution(final_total, direct_cost)
        if direct_cost is not None
        else None,
    )


def get_orders(
    db: Session,
    business: Business,
    period: Period,
    *,
    status: OrderStatusFilter = OrderStatusFilter.ALL,
    outstanding_only: bool = False,
    sort: OrderSort = OrderSort.REPORTED_AT,
    direction: SortDirection | None = None,
    limit: int,
    offset: int,
) -> OrdersReport:
    ro = _order_scope(business.id, period)
    completed = ro.c.status == OrderStatus.COMPLETED

    method_cols = []
    for method in _METHODS:
        cond = (
            ro.c.fulfillment_method.is_(None)
            if method is None
            else ro.c.fulfillment_method == method
        )
        method_cols.append(func.count().filter(and_(completed, cond)))
        method_cols.append(
            func.coalesce(func.sum(ro.c.final_total).filter(and_(completed, cond)), 0)
        )

    largest = (
        select(
            ro.c.id,
            ro.c.order_number,
            ro.c.final_total,
            ro.c.reported_at,
            ro.c.customer_name,
        )
        .where(completed)
        .order_by(ro.c.final_total.desc(), ro.c.reported_at.asc(), ro.c.id.asc())
        .limit(1)
        .cte("largest")
    )
    summary = (
        select(
            func.count().filter(completed).label("completed_orders"),
            func.count().filter(ro.c.status == OrderStatus.CANCELED).label("canceled_orders"),
            func.coalesce(func.sum(ro.c.final_total).filter(completed), 0).label("revenue"),
            func.count().filter(and_(completed, ro.c.balance > 0)).label("outstanding_count"),
            func.coalesce(
                func.sum(ro.c.balance).filter(and_(completed, ro.c.balance > 0)), 0
            ).label("outstanding_balance"),
            *[c.label(f"m{i}") for i, c in enumerate(method_cols)],
        )
        .select_from(ro)
        .cte("summary")
    )

    page = (
        select(ro)
        .where(*_order_filters(ro, status, outstanding_only))
        .order_by(*_order_order_by(sort, direction, ro.c))
        .limit(limit)
        .offset(offset)
        .subquery("page")
    )
    stmt = (
        select(
            summary,
            largest.c.id.label("l_id"),
            largest.c.order_number.label("l_number"),
            largest.c.final_total.label("l_total"),
            largest.c.reported_at.label("l_at"),
            largest.c.customer_name.label("l_customer"),
            page,
        )
        .select_from(summary.outerjoin(largest, true()).outerjoin(page, true()))
        .order_by(*_order_order_by(sort, direction, page.c))
    )
    result = db.execute(stmt).all()
    head = result[0]

    revenue = _money(head.revenue)
    completed_orders = int(head.completed_orders)
    canceled_orders = int(head.canceled_orders)
    outstanding_count = int(head.outstanding_count)

    methods: list[FulfillmentMethodRow] = []
    for i, method in enumerate(_METHODS):
        methods.append(
            FulfillmentMethodRow(
                method=method,
                completed_orders=int(getattr(head, f"m{2 * i}")),
                revenue=_money(getattr(head, f"m{2 * i + 1}")),
            )
        )

    if outstanding_only:
        total = 0 if status is OrderStatusFilter.CANCELED else outstanding_count
    elif status is OrderStatusFilter.COMPLETED:
        total = completed_orders
    elif status is OrderStatusFilter.CANCELED:
        total = canceled_orders
    else:
        total = completed_orders + canceled_orders

    largest_order = None
    if head.l_id is not None:
        largest_order = LargestOrder(
            order_id=head.l_id,
            order_number=head.l_number,
            final_total=_money(head.l_total),
            completed_at=head.l_at,
            customer_name=head.l_customer,
        )

    return OrdersReport(
        summary=OrdersSummary(
            completed_orders=completed_orders,
            canceled_orders=canceled_orders,
            revenue=revenue,
            average_order_value=kpi.average_order_value(revenue, completed_orders),
            largest_completed_order=largest_order,
            fulfillment_methods=methods,
            outstanding=OutstandingSummary(
                order_count=outstanding_count, balance=_money(head.outstanding_balance)
            ),
        ),
        items=[_order_row(r) for r in result if r.id is not None],
        total=total,
        limit=limit,
        offset=offset,
    )


def stream_orders(
    db: Session,
    business: Business,
    period: Period,
    *,
    status: OrderStatusFilter,
    outstanding_only: bool,
    sort: OrderSort,
    direction: SortDirection | None,
) -> Iterator[OrderRow]:
    """Every matching Order row (no paging, no ceiling), same total order as the JSON
    report, through a server-side cursor."""
    ro = _order_scope(business.id, period)
    stmt = (
        select(ro)
        .where(*_order_filters(ro, status, outstanding_only))
        .order_by(*_order_order_by(sort, direction, ro.c))
    )
    for r in db.execute(stmt.execution_options(yield_per=STREAM_CHUNK)):
        yield _order_row(r)
