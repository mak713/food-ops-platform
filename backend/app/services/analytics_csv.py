"""CSV surfaces for Analytics (Spec §13.9, ANA-012; Phase 10).

Every export is produced from the *same* service rows the JSON reports use
(`app.services.analytics_service`), converted cell-by-cell here, so tenant, period, ordering
and numbers are identical to the JSON/UI by construction. Customers and Orders are streamed
through a server-side cursor; Products and Trends are catalog/bucket-bounded aggregates.

Text cells (names, labels, order numbers) go through the formula-injection guard; numeric
cells never do (see `app.core.csv_export`).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from app.core.csv_export import Cell, Text
from app.services import analytics_service as svc

PRODUCT_COLUMNS = [
    "row_type",
    "product_id",
    "product_name",
    "product_type",
    "selling_option_id",
    "selling_option",
    "underlying_units",
    "package_count",
    "revenue",
    "direct_cost",
    "contribution",
    "contribution_margin",
    "completed_orders",
]

CUSTOMER_COLUMNS = [
    "row_type",
    "customer_id",
    "customer_name",
    "classification",
    "completed_orders",
    "revenue",
    "average_order_value",
    "share_of_completed_orders",
]

ORDER_COLUMNS = [
    "order_number",
    "status",
    "reported_at",
    "customer_id",
    "customer_name",
    "fulfillment_method",
    "final_total",
    "payments_total",
    "payment_status",
    "outstanding_balance",
    "direct_cost",
    "contribution",
]

TREND_COLUMNS = [
    "row_type",
    "bucket",
    "start_date",
    "end_date",
    "revenue",
    "direct_cost",
    "contribution",
    "completed_orders",
]


def iso_utc(value: datetime) -> str:
    """UTC ISO-8601 with a `Z` suffix — the same instant text the JSON API emits."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _id(value) -> Text | None:
    return None if value is None else Text(str(value))


def product_rows(report: svc.ProductsReport) -> Iterator[list[Cell]]:
    for p in report.rows:
        yield [
            Text("PRODUCT"),
            _id(p.product_id),
            Text(p.name),
            Text(p.product_type),
            None,
            None,
            p.underlying_units,
            None,
            p.revenue,
            p.direct_cost,
            p.contribution,
            p.contribution_margin,
            p.completed_orders,
        ]
        for o in p.selling_options:
            yield [
                Text("SELLING_OPTION"),
                _id(p.product_id),
                Text(p.name),
                Text(p.product_type),
                _id(o.selling_option_id),
                Text(o.name),
                o.underlying_units,
                o.package_count,
                o.revenue,
                o.direct_cost,
                o.contribution,
                o.contribution_margin,
                o.completed_orders,
            ]
    if report.custom_items is not None:
        c = report.custom_items
        yield [
            Text("CUSTOM_ITEMS"),
            None,
            Text("Custom items (no Product)"),
            None,
            None,
            None,
            None,
            None,
            c.revenue,
            c.direct_cost,
            c.contribution,
            c.contribution_margin,
            c.completed_orders,
        ]
    lvl = report.order_level
    yield [
        Text("ORDER_ADJUSTMENT"),
        None,
        Text("Order adjustments"),
        None,
        None,
        None,
        None,
        None,
        lvl.order_adjustments,
        None,
        None,
        None,
        None,
    ]
    yield [
        Text("MANUAL_TAX"),
        None,
        Text("Manual tax"),
        None,
        None,
        None,
        None,
        None,
        lvl.manual_tax,
        None,
        None,
        None,
        None,
    ]
    # Defensive reconciliation category (Revenue not explained by Product lines, Custom Items,
    # adjustments and tax — e.g. a line whose Product reference does not resolve tenant-locally).
    # Always emitted, even at 0.00, so the export shape is deterministic and the top-level rows
    # sum to TOTAL.revenue. Never assigned to a Product; no cost/contribution is derived for it.
    yield [
        Text("UNRECONCILED_REVENUE"),
        None,
        Text("Unreconciled revenue"),
        None,
        None,
        None,
        None,
        None,
        lvl.unreconciled_revenue,
        None,
        None,
        None,
        None,
    ]
    yield [
        Text("UNATTRIBUTED_DIRECT_COST"),
        None,
        Text("Unattributed direct cost"),
        None,
        None,
        None,
        None,
        None,
        None,
        lvl.unattributed_direct_cost,
        None,
        None,
        None,
    ]
    t = report.totals
    yield [
        Text("TOTAL"),
        None,
        Text("All completed Orders"),
        None,
        None,
        None,
        None,
        None,
        t.revenue,
        t.direct_cost,
        t.contribution,
        t.contribution_margin,
        t.completed_orders,
    ]


def customer_rows(items: Iterator[svc.CustomerRow | svc.GuestSummary]) -> Iterator[list[Cell]]:
    """Consumes the single-statement export stream: Customer rows, then the Guest summary."""
    for item in items:
        if isinstance(item, svc.GuestSummary):
            yield [
                Text("GUEST_SUMMARY"),
                None,
                Text("Guest orders"),
                None,
                item.completed_orders,
                item.revenue,
                item.average_order_value,
                item.share_of_completed_orders,
            ]
        else:
            yield [
                Text("CUSTOMER"),
                _id(item.customer_id),
                Text(item.name),
                Text(item.classification.value),
                item.completed_orders,
                item.revenue,
                item.average_order_value,
                None,
            ]


def order_rows(rows: Iterator[svc.OrderRow]) -> Iterator[list[Cell]]:
    for o in rows:
        yield [
            Text(o.order_number),
            Text(o.status.value),
            Text(iso_utc(o.reported_at)),
            _id(o.customer_id),
            Text(o.customer_name),
            Text(o.fulfillment_method.value if o.fulfillment_method else None),
            o.final_total,
            o.payments_total,
            Text(o.payment_status.value if o.payment_status else None),
            o.outstanding_balance,
            o.direct_cost,
            o.contribution,
        ]


def trend_rows(report: svc.TrendsReport) -> Iterator[list[Cell]]:
    for b in report.buckets:
        yield [
            Text("BUCKET"),
            Text(b.bucket.label),
            Text(b.bucket.start_date.isoformat()),
            Text(b.bucket.end_date.isoformat()),
            b.revenue,
            b.direct_cost,
            b.contribution,
            b.completed_orders,
        ]
    t = report.totals
    yield [
        Text("TOTAL"),
        None,
        None,
        None,
        t.revenue,
        t.direct_cost,
        t.contribution,
        t.completed_orders,
    ]
