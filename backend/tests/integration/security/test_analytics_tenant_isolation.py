"""Phase 10 Analytics tenant isolation (Spec §19.14, AC-SEC-005; release-blocking).

Strongest available proof, as in Phase 9: build Business A's world, capture every A response
(JSON and CSV), add a larger overlapping Business B world, and prove A's responses are
**byte-for-byte unchanged** and B's never contain any of A's identifiers or names. Cross-tenant
*corruption* (rows whose own `business_id` differs from the row they point at — the schema has
no composite tenant FKs) is also injected to prove every table in every query graph carries its
own tenant predicate.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.enums import CostType, FulfillmentMethod, OrderLineType
from app.db.models.order import OrderLine
from tests.integration.api.analytics_support import (
    Builder,
    P,
    at,
    build_golden,
    custom_item,
    custom_qty,
    freeze,
)
from tests.integration.api.helpers import signup
from tests.integration.api.phase9_support import business_by_name
from tests.integration.schema.factories import (
    make_order_cost_allocation,
    make_order_line,
    make_payment,
)

D = Decimal
BASE = "/api/v1/analytics"
P7 = {"range": "LAST_7_DAYS"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

REQUESTS = [
    (f"{BASE}/overview", {**P7, "compare": "true"}),
    (f"{BASE}/trends", P7),
    (f"{BASE}/products", P7),
    (f"{BASE}/customers", P7),
    (f"{BASE}/orders", P7),
    (f"{BASE}/products.csv", P7),
    (f"{BASE}/customers.csv", P7),
    (f"{BASE}/orders.csv", P7),
    (f"{BASE}/trends.csv", P7),
]


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    freeze(monkeypatch)


def _signup_two(client):
    signup(
        client,
        email="ana-a@example.com",
        business_name="Bakery A",
        business_timezone="America/Chicago",
    )
    a = {"session": client.cookies.get("fo_session"), "csrf": client.cookies.get("fo_csrf")}
    client.cookies.clear()
    signup(
        client,
        email="ana-b@example.com",
        business_name="Bakery B",
        business_timezone="America/Chicago",
    )
    b = {"session": client.cookies.get("fo_session"), "csrf": client.cookies.get("fo_csrf")}
    return a, b


def _use(client, tenant: dict) -> None:
    client.cookies.clear()
    client.cookies.set("fo_session", tenant["session"])
    client.cookies.set("fo_csrf", tenant["csrf"])


def _snapshot(client) -> dict[str, bytes]:
    out = {}
    for path, params in REQUESTS:
        response = client.get(path, params=params)
        assert response.status_code == 200, (path, response.text)
        out[f"{path}?{sorted(params.items())}"] = response.content
    return out


def _build_b_world(session, business) -> dict:
    """Larger, overlapping-shape world: same product/customer *names*, same dates, huge numbers."""
    b = Builder(session, business)
    cust = b.customer("Alice")
    secret = b.customer("SECRET-B-CUSTOMER")
    prod = b.product("Cookie")
    secret_prod = b.product("SECRET-B-PRODUCT")
    option = b.option(prod, "6-pack", "6", "12.00")
    orders = []
    for i in range(12):
        orders.append(
            b.order(
                completed_at=at(10 + i % 7, 12),
                customer=cust if i % 2 else secret,
                method=FulfillmentMethod.DELIVERY,
                number=f"B-{i}",
                payments=["1.00"],
                lines=[
                    _std_line(prod, option),
                    custom_qty(
                        secret_prod, "3", "777.00", {CostType.PACKAGING: "111.00", P: "5.00"}
                    ),
                    custom_item("9.00", {CostType.CUSTOM_DIRECT: "4.00"}),
                ],
            )
        )
    guest = b.order(
        completed_at=at(14, 12),
        customer=None,
        number="B-GUEST",
        lines=[custom_qty(prod, "1", "50000.00", {CostType.PACKAGING: "1.00"})],
    )
    session.commit()
    return {
        "orders": orders,
        "guest": guest,
        "prod": prod,
        "secret": secret,
        "secret_prod": secret_prod,
        "option": option,
        "alice": cust,
    }


def _std_line(prod, option):
    from tests.integration.api.analytics_support import std

    return std(prod, option, "2", "12", "300.00", {P: "50.00", CostType.PACKAGING: "2.00"})


def test_other_tenants_data_never_changes_a_tenants_reports(client, session):
    a, b = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    golden = build_golden(session, biz_a)

    _use(client, a)
    before = _snapshot(client)
    assert b"166.00" in before[f"{BASE}/overview?{sorted({**P7, 'compare': 'true'}.items())}"]

    _build_b_world(session, biz_b)

    _use(client, a)
    after = _snapshot(client)
    assert after == before  # byte-for-byte identical

    # And B's own reports are non-trivial yet contain nothing of A's.
    _use(client, b)
    b_snapshot = _snapshot(client)
    a_ids = {str(o.id) for o in golden.orders.values()} | {
        str(c.id) for c in (golden.alice, golden.bob, golden.cara)
    }
    a_ids |= {str(golden.cookie.id), str(golden.brownie.id), str(golden.lemonade.id)}
    a_ids |= {str(golden.six_pack.id), str(golden.twelve_pack.id), str(golden.bottle.id)}
    for key, content in b_snapshot.items():
        text_body = content.decode("utf-8-sig")
        assert not (set(UUID_RE.findall(text_body)) & a_ids), key
        for marker in ("Bob", "Cara", "Brownie", "Lemonade", "O1", "O2", "CANC-IN", "12-pack"):
            assert marker not in text_body, (key, marker)
    overview_b = client.get(f"{BASE}/overview", params=P7).json()["current"]
    assert overview_b["completed_orders"] == 13
    assert overview_b["revenue"] != "166.00"


def test_each_tenant_sees_only_its_own_overlapping_names(client, session):
    a, b = _signup_two(client)
    build_golden(session, business_by_name(session, "Bakery A"))
    _build_b_world(session, business_by_name(session, "Bakery B"))

    _use(client, a)
    customers_a = client.get(f"{BASE}/customers", params=P7).json()["items"]
    assert {c["customer_name"] for c in customers_a} == {"Alice", "Bob", "Cara"}
    _use(client, b)
    customers_b = client.get(f"{BASE}/customers", params=P7).json()["items"]
    assert {c["customer_name"] for c in customers_b} == {"Alice", "SECRET-B-CUSTOMER"}
    assert not ({c["customer_id"] for c in customers_a} & {c["customer_id"] for c in customers_b})


def test_cross_tenant_corrupted_rows_never_enter_either_tenants_reports(client, session):
    a, b = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    golden = build_golden(session, biz_a)
    world_b = _build_b_world(session, biz_b)

    _use(client, a)
    a_before = _snapshot(client)
    _use(client, b)
    b_before = _snapshot(client)

    o_a = golden.orders["O2"]
    o_b = world_b["orders"][0]

    # (1) A ledger row owned by A that points at B's Order / (2) one owned by B pointing at A's.
    make_order_cost_allocation(session, biz_a, o_b, cost_type=CostType.PACKAGING, amount="999.00")
    make_order_cost_allocation(session, biz_b, o_a, cost_type=CostType.PACKAGING, amount="888.00")
    # (3) A line owned by B hanging off A's Order, and one owned by A hanging off B's Order.
    make_order_line(
        session,
        biz_b,
        o_a,
        line_type=OrderLineType.CUSTOM_ITEM,
        line_subtotal="5000.00",
        underlying_quantity=1,
    )
    make_order_line(
        session,
        biz_a,
        o_b,
        line_type=OrderLineType.CUSTOM_ITEM,
        line_subtotal="6000.00",
        underlying_quantity=1,
    )
    # (4) Payments owned by the other tenant recorded against each Order.
    make_payment(session, biz_b, o_a, amount="32.00")
    make_payment(session, biz_a, o_b, amount="99.00")
    session.commit()

    _use(client, a)
    assert _snapshot(client) == a_before
    _use(client, b)
    assert _snapshot(client) == b_before


def test_unresolved_foreign_references_never_leak_ids_or_names_and_totals_reconcile(
    client, session
):
    """Remediation Finding 1 (release-blocking). Tenant A's own rows are corrupted to reference
    tenant B's Customer, Product and Selling Option. Every Analytics JSON and CSV response must
    contain none of B's identifiers (searched as UUID strings) or names; A's Overview figures are
    unchanged and the Product table reconciles through the defensive reconciliation values."""
    a, b = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    golden = build_golden(session, biz_a)
    world_b = _build_b_world(session, biz_b)

    _use(client, a)
    baseline_overview = client.get(f"{BASE}/overview", params=P7).json()["current"]
    assert baseline_overview["revenue"] == "166.00"

    foreign_customer = world_b["secret"]  # "SECRET-B-CUSTOMER"
    foreign_customer_2 = world_b["alice"]
    foreign_product = world_b["secret_prod"]  # "SECRET-B-PRODUCT"
    foreign_option = world_b["option"]

    # Customer references: O4 (Alice, 24.00) and O2 (Bob, 72.00, the largest Order).
    golden.orders["O4"].customer_id = foreign_customer.id
    golden.orders["O2"].customer_id = foreign_customer_2.id

    # Product reference: O1's Brownie line (revenue 20.00, cost 6.25) now points at B's Product.
    o1_brownie = session.scalars(
        select(OrderLine).where(
            OrderLine.order_id == golden.orders["O1"].id,
            OrderLine.product_id == golden.brownie.id,
        )
    ).one()
    o1_brownie.product_id = foreign_product.id

    # Selling Option references on O4's two Cookie 6-pack lines: one points at B's Option, the
    # other at an Option of A's *other* Product (exists in A, but not under the line's Product).
    o4_lines = session.scalars(
        select(OrderLine)
        .where(OrderLine.order_id == golden.orders["O4"].id)
        .order_by(OrderLine.created_at, OrderLine.id)
    ).all()
    assert len(o4_lines) == 2
    o4_lines[0].selling_option_id = foreign_option.id
    o4_lines[1].selling_option_id = golden.bottle.id
    session.commit()

    foreign_ids = {
        str(foreign_customer.id),
        str(foreign_customer_2.id),
        str(foreign_product.id),
        str(foreign_option.id),
        str(world_b["prod"].id),
        str(world_b["guest"].id),
    } | {str(o.id) for o in world_b["orders"]}
    foreign_markers = ("SECRET-B", "B-GUEST", "B-0", "B-1")

    for path, params in REQUESTS:
        body = client.get(path, params=params).content.decode("utf-8-sig")
        for foreign_id in foreign_ids:
            assert foreign_id not in body, (path, "foreign UUID leaked", foreign_id)
        for marker in foreign_markers:
            assert marker not in body, (path, marker)

    # Overview/Order revenue still includes A's own Orders.
    overview = client.get(f"{BASE}/overview", params=P7).json()["current"]
    assert overview == baseline_overview

    # Product table: the quarantined Brownie line is not a Product row; its Revenue is visible
    # as unreconciled revenue and its line-attributed cost is folded into unattributed cost.
    products = client.get(f"{BASE}/products", params=P7).json()
    names = {r["product_name"]: r for r in products["rows"]}
    assert set(names) == {"Cookie", "Brownie", "Lemonade"}
    assert names["Brownie"]["revenue"] == "7.00"
    assert names["Brownie"]["direct_cost"] == "3.50"
    assert names["Brownie"]["underlying_units"] == "7.000000"
    assert products["order_level"]["unreconciled_revenue"] == "20.00"
    assert products["order_level"]["unattributed_direct_cost"] == "6.25"
    assert products["totals"]["revenue"] == "166.00"
    assert products["totals"]["direct_cost"] == "47.05"
    rows_revenue = sum(D(r["revenue"]) for r in products["rows"])
    rows_cost = sum(D(r["direct_cost"]) for r in products["rows"])
    ci = products["custom_items"]
    lvl = products["order_level"]
    assert rows_revenue + D(ci["revenue"]) + D(lvl["order_adjustments"]) + D(lvl["manual_tax"]) + D(
        lvl["unreconciled_revenue"]
    ) == D(products["totals"]["revenue"])
    assert rows_cost + D(ci["direct_cost"]) + D(lvl["unattributed_direct_cost"]) == D(
        products["totals"]["direct_cost"]
    )
    assert products["most_profitable"]["product_id"] == str(golden.cookie.id)

    # Selling Option breakdown: unresolved Standard Options aggregate into one safe entry
    # (not Custom Quantity, no foreign id).
    cookie = names["Cookie"]
    by_name = {o["name"]: o for o in cookie["selling_options"]}
    unavailable = by_name["(unavailable selling option)"]
    assert unavailable["kind"] == "STANDARD_OPTION"
    assert unavailable["selling_option_id"] is None
    assert unavailable["package_count"] == "2.000000"
    assert unavailable["underlying_units"] == "12.000000"
    assert (unavailable["revenue"], unavailable["direct_cost"]) == ("24.00", "6.60")
    assert unavailable["completed_orders"] == 1
    assert by_name["Custom quantity"]["kind"] == "CUSTOM_QUANTITY"
    assert by_name["Custom quantity"]["underlying_units"] == "30.000000"
    assert by_name["6-pack"]["package_count"] == "2.000000"  # only O1's line still resolves

    # Customers: the unresolved references are neither Customers nor Guests.
    customers = client.get(f"{BASE}/customers", params=P7).json()
    assert {c["customer_name"] for c in customers["items"]} == {"Alice", "Bob", "Cara"}
    by_customer = {c["customer_name"]: c for c in customers["items"]}
    assert (by_customer["Alice"]["completed_orders"], by_customer["Alice"]["revenue"]) == (
        1,
        "46.00",
    )
    assert (by_customer["Bob"]["completed_orders"], by_customer["Bob"]["revenue"]) == (1, "3.00")
    assert customers["summary"]["unique_customers"] == 3
    assert customers["summary"]["guest"]["completed_orders"] == 2  # still strictly NULL ids

    # Orders: the id is withheld and the label is safe — and it is NOT "Guest".
    orders = client.get(f"{BASE}/orders", params=P7).json()
    rows = {i["order_number"]: i for i in orders["items"]}
    for number in ("O4", "O2"):
        assert rows[number]["customer_id"] is None
        assert rows[number]["customer_name"] == "(unavailable customer)"
    assert rows["O3"]["customer_name"] is None and rows["O3"]["customer_id"] is None  # Guest
    largest = orders["summary"]["largest_completed_order"]
    assert largest["order_number"] == "O2"
    assert largest["customer_name"] == "(unavailable customer)"
    assert orders["summary"]["revenue"] == "166.00"

    orders_csv = client.get(f"{BASE}/orders.csv", params=P7).content.decode("utf-8-sig")
    o4_line = next(line for line in orders_csv.splitlines() if line.startswith("O4,"))
    cells = next(csv.reader([o4_line]))
    assert cells[3] == "" and cells[4] == "(unavailable customer)"

    # Product CSV after the foreign-Product quarantine: the quarantined revenue/cost are
    # exported explicitly, the foreign Product is absent (the byte-level UUID/name scan above
    # already covered products.csv), and the top-level rows reconcile exactly to TOTAL.
    product_rows = list(
        csv.DictReader(
            io.StringIO(
                client.get(f"{BASE}/products.csv", params=P7).content.decode("utf-8-sig"),
                newline="",
            )
        )
    )
    by_type = {
        r["row_type"]: r for r in product_rows if r["row_type"] not in ("PRODUCT", "SELLING_OPTION")
    }
    assert by_type["UNRECONCILED_REVENUE"]["revenue"] == "20.00"
    assert by_type["UNRECONCILED_REVENUE"]["product_id"] == ""
    assert by_type["UNATTRIBUTED_DIRECT_COST"]["direct_cost"] == "6.25"
    assert by_type["TOTAL"]["revenue"] == "166.00"
    assert by_type["TOTAL"]["direct_cost"] == "47.05"
    assert not any(r["product_name"] == "SECRET-B-PRODUCT" for r in product_rows)
    revenue_types = (
        "PRODUCT",
        "CUSTOM_ITEMS",
        "ORDER_ADJUSTMENT",
        "MANUAL_TAX",
        "UNRECONCILED_REVENUE",
    )
    cost_types = ("PRODUCT", "CUSTOM_ITEMS", "UNATTRIBUTED_DIRECT_COST")
    assert sum(
        D(r["revenue"]) for r in product_rows if r["row_type"] in revenue_types and r["revenue"]
    ) == D("166.00")
    assert sum(
        D(r["direct_cost"])
        for r in product_rows
        if r["row_type"] in cost_types and r["direct_cost"]
    ) == D("47.05")


def test_unauthenticated_and_other_session_cannot_read(client, session):
    a, b = _signup_two(client)
    build_golden(session, business_by_name(session, "Bakery A"))
    client.cookies.clear()
    for path, params in REQUESTS:
        assert client.get(path, params=params).status_code == 401
