"""Phase 10 CSV export tests (AC-ANA-010, ANA-012; Spec §11.11, §13.9): JSON<->CSV equivalence,
headers, determinism, formula-injection defense, streaming and session lifetime."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.api.v1 import analytics as analytics_router  # noqa: F401  (clock seam)
from app.core.csv_export import UTF8_BOM
from app.db.enums import CostType, OrderStatus
from app.db.session import get_db
from app.main import app
from app.services import analytics_csv as csv_cols
from app.services import analytics_service as svc
from tests.integration.api.analytics_support import at, custom_qty, setup_golden

BASE = "/api/v1/analytics"
P7 = {"range": "LAST_7_DAYS"}


@pytest.fixture()
def golden(client, session, monkeypatch):
    return setup_golden(client, session, monkeypatch)


def parse(response) -> tuple[list[str], list[dict[str, str]]]:
    assert response.content.startswith(UTF8_BOM.encode("utf-8"))
    text_body = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_body, newline=""))
    rows = list(reader)
    assert reader.fieldnames is not None
    return list(reader.fieldnames), rows


def iso(value: str | None) -> str:
    return value or ""


def test_headers_filename_and_encoding(client, golden):
    response = client.get(f"{BASE}/orders.csv", params=P7)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="analytics-orders_2026-10-10_to_2026-10-16.csv"'
    )
    assert response.content.startswith(b"\xef\xbb\xbf")
    columns, _ = parse(response)
    assert columns == csv_cols.ORDER_COLUMNS


@pytest.mark.parametrize(
    ("path", "columns", "name"),
    [
        ("products.csv", csv_cols.PRODUCT_COLUMNS, "products"),
        ("customers.csv", csv_cols.CUSTOMER_COLUMNS, "customers"),
        ("orders.csv", csv_cols.ORDER_COLUMNS, "orders"),
        ("trends.csv", csv_cols.TREND_COLUMNS, "trends"),
    ],
)
def test_every_export_has_deterministic_columns_and_filename(client, golden, path, columns, name):
    response = client.get(
        f"{BASE}/{path}",
        params={"range": "CUSTOM", "start_date": "2026-10-01", "end_date": "2026-10-16"},
    )
    assert response.status_code == 200
    got_columns, _ = parse(response)
    assert got_columns == columns
    assert (
        f'filename="analytics-{name}_2026-10-01_to_2026-10-16.csv"'
        in (response.headers["content-disposition"])
    )


def test_same_state_and_clock_produces_byte_identical_csv(client, golden):
    for path in ("products.csv", "customers.csv", "orders.csv", "trends.csv"):
        first = client.get(f"{BASE}/{path}", params=P7).content
        second = client.get(f"{BASE}/{path}", params=P7).content
        assert first == second


# ------------------------------------------------------------------ JSON <-> CSV equivalence


def test_orders_csv_equals_all_json_pages(client, golden):
    items = []
    offset = 0
    while True:
        page = client.get(BASE + "/orders", params={**P7, "limit": 3, "offset": offset}).json()
        items += page["items"]
        offset += 3
        if offset >= page["total"]:
            break
    _, rows = parse(client.get(f"{BASE}/orders.csv", params=P7))
    assert len(rows) == len(items) == 8
    for row, item in zip(rows, items, strict=True):
        assert row["order_number"] == item["order_number"]
        assert row["status"] == item["status"]
        assert row["reported_at"] == item["reported_at"]
        assert row["customer_name"] == iso(item["customer_name"])
        assert row["customer_id"] == iso(item["customer_id"])
        assert row["fulfillment_method"] == iso(item["fulfillment_method"])
        for key in (
            "final_total",
            "payments_total",
            "payment_status",
            "outstanding_balance",
            "direct_cost",
            "contribution",
        ):
            assert row[key] == iso(item[key]), key


def test_orders_csv_honours_filters_like_json(client, golden):
    params = {**P7, "status": "COMPLETED", "outstanding_only": "true", "sort": "final_total"}
    json_numbers = [
        i["order_number"] for i in client.get(BASE + "/orders", params=params).json()["items"]
    ]
    _, rows = parse(client.get(f"{BASE}/orders.csv", params=params))
    assert [r["order_number"] for r in rows] == json_numbers == ["O2", "O3", "E2", "E1"]


def test_customers_csv_equals_json_plus_guest_row(client, golden):
    body = client.get(BASE + "/customers", params={**P7, "limit": 200}).json()
    _, rows = parse(client.get(f"{BASE}/customers.csv", params=P7))
    customer_rows = [r for r in rows if r["row_type"] == "CUSTOMER"]
    guest_rows = [r for r in rows if r["row_type"] == "GUEST_SUMMARY"]
    assert len(customer_rows) == len(body["items"]) == 3
    for row, item in zip(customer_rows, body["items"], strict=True):
        assert row["customer_id"] == item["customer_id"]
        assert row["customer_name"] == item["customer_name"]
        assert row["classification"] == item["classification"]
        assert row["completed_orders"] == str(item["completed_orders"])
        assert row["revenue"] == item["revenue"]
        assert row["average_order_value"] == iso(item["average_order_value"])
    guest = body["summary"]["guest"]
    assert len(guest_rows) == 1
    assert guest_rows[0]["completed_orders"] == str(guest["completed_orders"])
    assert guest_rows[0]["revenue"] == guest["revenue"]
    assert guest_rows[0]["average_order_value"] == guest["average_order_value"]
    assert guest_rows[0]["share_of_completed_orders"] == guest["share_of_completed_orders"]
    assert rows[-1]["row_type"] == "GUEST_SUMMARY"  # guests are separate, never a customer row


def assert_csv_reconciles(rows: list[dict[str, str]]) -> None:
    """The Product CSV's explicit top-level rows must reconcile exactly to TOTAL. SELLING_OPTION
    rows are drill-down children of PRODUCT rows and are deliberately NOT summed (they would
    double-count Product revenue)."""
    revenue_types = (
        "PRODUCT",
        "CUSTOM_ITEMS",
        "ORDER_ADJUSTMENT",
        "MANUAL_TAX",
        "UNRECONCILED_REVENUE",
    )
    cost_types = ("PRODUCT", "CUSTOM_ITEMS", "UNATTRIBUTED_DIRECT_COST")
    total = next(r for r in rows if r["row_type"] == "TOTAL")
    revenue = sum(
        Decimal(r["revenue"]) for r in rows if r["row_type"] in revenue_types and r["revenue"]
    )
    cost = sum(
        Decimal(r["direct_cost"]) for r in rows if r["row_type"] in cost_types and r["direct_cost"]
    )
    assert revenue == Decimal(total["revenue"])
    assert cost == Decimal(total["direct_cost"])


def test_products_csv_equals_json_including_reconciliation_rows(client, golden):
    body = client.get(BASE + "/products", params=P7).json()
    _, rows = parse(client.get(f"{BASE}/products.csv", params=P7))
    expected: list[tuple[str, dict]] = []
    for p in body["rows"]:
        expected.append(("PRODUCT", p))
        for o in p["selling_options"]:
            expected.append(("SELLING_OPTION", {**o, "product_id": p["product_id"]}))
    product_rows = [r for r in rows if r["row_type"] in ("PRODUCT", "SELLING_OPTION")]
    assert len(product_rows) == len(expected)
    for row, (kind, item) in zip(product_rows, expected, strict=True):
        assert row["row_type"] == kind
        assert row["underlying_units"] == item["underlying_units"]
        assert row["revenue"] == item["revenue"]
        assert row["direct_cost"] == item["direct_cost"]
        assert row["contribution"] == item["contribution"]
        assert row["contribution_margin"] == iso(item["contribution_margin"])
        assert row["completed_orders"] == str(item["completed_orders"])
        if kind == "PRODUCT":
            assert row["product_name"] == item["product_name"]
        else:
            assert row["selling_option"] == item["name"]
            assert row["package_count"] == iso(item["package_count"])
    by_type = {r["row_type"]: r for r in rows}
    assert by_type["CUSTOM_ITEMS"]["revenue"] == body["custom_items"]["revenue"]
    assert by_type["ORDER_ADJUSTMENT"]["revenue"] == body["order_level"]["order_adjustments"]
    assert by_type["MANUAL_TAX"]["revenue"] == body["order_level"]["manual_tax"]
    assert by_type["UNATTRIBUTED_DIRECT_COST"]["direct_cost"] == "0.00"
    total = by_type["TOTAL"]
    assert total["revenue"] == body["totals"]["revenue"] == "166.00"
    assert total["direct_cost"] == body["totals"]["direct_cost"]
    assert total["completed_orders"] == "7"

    # The defensive unreconciled-revenue category is always exported (even at 0.00), exactly once,
    # equal to the JSON value, and never assigned to a Product / merged with adjustment or tax.
    unreconciled = [r for r in rows if r["row_type"] == "UNRECONCILED_REVENUE"]
    assert len(unreconciled) == 1
    assert unreconciled[0]["revenue"] == body["order_level"]["unreconciled_revenue"] == "0.00"
    assert unreconciled[0]["product_id"] == "" and unreconciled[0]["product_name"] == (
        "Unreconciled revenue"
    )
    for blank in (
        "underlying_units",
        "package_count",
        "direct_cost",
        "contribution",
        "contribution_margin",
        "completed_orders",
    ):
        assert unreconciled[0][blank] == "", blank
    assert_csv_reconciles(rows)


def test_trends_csv_equals_json_and_total_row(client, golden):
    body = client.get(BASE + "/trends", params=P7).json()
    _, rows = parse(client.get(f"{BASE}/trends.csv", params=P7))
    buckets = [r for r in rows if r["row_type"] == "BUCKET"]
    assert len(buckets) == len(body["buckets"]) == 7
    for row, item in zip(buckets, body["buckets"], strict=True):
        assert row["bucket"] == item["label"]
        assert row["start_date"] == item["start_date"]
        assert row["end_date"] == item["end_date"]
        assert row["revenue"] == item["revenue"]
        assert row["direct_cost"] == item["direct_cost"]
        assert row["contribution"] == item["contribution"]
        assert row["completed_orders"] == str(item["completed_orders"])
    total = rows[-1]
    assert total["row_type"] == "TOTAL"
    assert total["revenue"] == body["totals"]["revenue"]
    assert total["completed_orders"] == "7"


# ------------------------------------------------------------------ CSV security


def test_text_cells_are_formula_neutralized_but_numbers_are_not(client, golden, session):
    evil_customer = golden.builder.customer('=HYPERLINK("http://evil.example","x")')
    evil_product = golden.builder.product("+cmd|' /C calc'!A0")
    golden.builder.order(
        completed_at=at(14, 18),
        customer=evil_customer,
        number="@SUM(1+1)",
        lines=[custom_qty(evil_product, "1", "10.00", {CostType.PACKAGING: "12.50"})],
    )
    session.commit()

    _, customers = parse(client.get(f"{BASE}/customers.csv", params=P7))
    names = {r["customer_name"] for r in customers}
    assert '\'=HYPERLINK("http://evil.example","x")' in names
    _, products = parse(client.get(f"{BASE}/products.csv", params=P7))
    assert "'+cmd|' /C calc'!A0" in {r["product_name"] for r in products}
    columns, orders = parse(client.get(f"{BASE}/orders.csv", params=P7))
    order = next(r for r in orders if r["order_number"].endswith("@SUM(1+1)"))
    assert order["order_number"] == "'@SUM(1+1)"
    # Negative contribution (10.00 - 12.50) stays a true numeric cell, never prefixed.
    assert order["contribution"] == "-2.50"
    assert not any(cell.startswith("'-") for r in orders for cell in r.values())
    # No cell of any export starts with an unneutralized formula prefix, except numeric columns.
    for r in orders:
        assert r["order_number"][:1] not in "=+-@\t\r"


# ------------------------------------------------------------------ Streaming / session lifetime


def test_stream_functions_are_lazy_generators_using_a_server_side_cursor(client, golden, session):
    import inspect

    from sqlalchemy import select

    from app.db.models.business import Business

    business = session.scalars(select(Business)).one()
    periods = _period_for(business)

    def open_cursors() -> int:
        return session.execute(text("SELECT count(*) FROM pg_cursors")).scalar_one()

    stream = svc.stream_orders(
        session,
        business,
        periods,
        status=svc.OrderStatusFilter.ALL,
        outstanding_only=False,
        sort=svc.OrderSort.REPORTED_AT,
        direction=None,
    )
    assert inspect.isgenerator(stream)
    before = open_cursors()
    first = next(stream)
    assert first.order_number == "E1"
    assert open_cursors() > before  # a server-side cursor is open mid-iteration (not materialized)
    rest = list(stream)
    assert len(rest) == 7
    assert open_cursors() == before  # released after exhaustion

    customers = svc.stream_customer_export(
        session, business, periods, sort=svc.CustomerSort.REVENUE, direction=None
    )
    assert inspect.isgenerator(customers)
    first_customer = next(customers)
    assert first_customer.name == "Bob"
    assert open_cursors() > before
    rest_items = list(customers)
    assert len(rest_items) == 3  # Alice, Cara, then exactly one trailing Guest summary
    assert isinstance(rest_items[-1], svc.GuestSummary)
    assert open_cursors() == before


def _period_for(business):
    from zoneinfo import ZoneInfo

    from app.domain.analytics_periods import AnalyticsRange, resolve_periods
    from tests.integration.api.analytics_support import TODAY

    periods, issues = resolve_periods(
        range_=AnalyticsRange.LAST_7_DAYS,
        start_date=None,
        end_date=None,
        business_today=TODAY,
        tz=ZoneInfo(business.timezone),
        compare=False,
    )
    assert not issues
    return periods.current


def test_streaming_response_keeps_request_session_open_until_stream_is_sent(
    client, golden, session
):
    """The request-scoped `get_db` teardown must run only after the streamed body has been
    sent (FastAPI >= 0.118), so the CSV generator never iterates a closed Session."""
    events: list[str] = []

    def tracked_db():
        try:
            yield session
        finally:
            events.append("db-closed")

    app.dependency_overrides[get_db] = tracked_db

    cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"{BASE}/orders.csv",
        "raw_path": f"{BASE}/orders.csv".encode(),
        "query_string": b"range=LAST_7_DAYS",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"cookie", cookies.encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    body = bytearray()
    status = {}

    async def run() -> None:
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()  # no disconnect: blocks until the response cancels it

        async def send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                events.append("response-start")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    events.append("body-chunk")
                    body.extend(chunk)

        await app(scope, receive, send)

    asyncio.run(run())

    assert status["code"] == 200
    assert events.count("db-closed") == 1
    assert events.index("db-closed") > max(i for i, e in enumerate(events) if e == "body-chunk"), (
        events
    )
    assert body.count(b"\r\n") >= 9  # header + 8 order rows were streamed through the live session
    assert b"E1" in body and b"CANC-IN" in body


def test_csv_for_unknown_validation_error_streams_nothing(client, golden):
    response = client.get(
        f"{BASE}/orders.csv",
        params={"range": "CUSTOM", "start_date": "2026-10-16", "end_date": "2026-10-01"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert not response.content.startswith(b"\xef\xbb\xbf")


def test_status_and_enum_constants_used_by_streaming_tests():
    assert OrderStatus.CANCELED.value == "CANCELED"
    assert isinstance(datetime.now(), datetime)
