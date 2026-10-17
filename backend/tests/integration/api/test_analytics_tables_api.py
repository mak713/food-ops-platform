"""Phase 10 Products / Customers / Orders API tests against `Golden`
(AC-ANA-006, AC-ANA-007, AC-ANA-008; Spec §13.5–13.7)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.enums import CostType, FulfillmentMethod, OrderStatus
from tests.integration.api.analytics_support import (
    at,
    custom_qty,
    setup_golden,
)
from tests.integration.schema.factories import make_order_cost_allocation, make_payment

D = Decimal
PRODUCTS = "/api/v1/analytics/products"
CUSTOMERS = "/api/v1/analytics/customers"
ORDERS = "/api/v1/analytics/orders"
OVERVIEW = "/api/v1/analytics/overview"
P7 = {"range": "LAST_7_DAYS"}


@pytest.fixture()
def golden(client, session, monkeypatch):
    return setup_golden(client, session, monkeypatch)


# ------------------------------------------------------------------ Products


def test_product_rows_units_vs_packages_and_option_breakdown(client, golden):
    body = client.get(PRODUCTS, params=P7).json()
    assert [r["product_name"] for r in body["rows"]] == ["Cookie", "Brownie", "Lemonade"]
    cookie, brownie, lemonade = body["rows"]

    assert cookie["product_id"] == str(golden.cookie.id)
    assert cookie["product_type"] == "PRODUCED"
    assert cookie["underlying_units"] == "66.000000"
    assert cookie["revenue"] == "125.00"
    assert cookie["direct_cost"] == "34.60"
    assert cookie["contribution"] == "90.40"
    assert cookie["contribution_margin"] == "0.723200"
    assert cookie["completed_orders"] == 3  # O1, O2 (two Cookie lines), O4 (two lines): distinct

    six, twelve, custom = cookie["selling_options"]
    assert (six["kind"], six["name"]) == ("STANDARD_OPTION", "6-pack")
    assert six["selling_option_id"] == str(golden.six_pack.id)
    assert six["package_count"] == "4.000000"  # 2 (O1) + 1 + 1 (O4): packages
    assert six["underlying_units"] == "24.000000"  # units are separate: 4 packages x 6
    assert (six["revenue"], six["direct_cost"], six["contribution"]) == ("48.00", "13.10", "34.90")
    assert six["contribution_margin"] == "0.727083"
    assert six["completed_orders"] == 2
    assert (twelve["name"], twelve["package_count"], twelve["underlying_units"]) == (
        "12-pack",
        "1.000000",
        "12.000000",
    )
    assert (twelve["revenue"], twelve["direct_cost"]) == ("22.00", "7.50")
    assert twelve["contribution_margin"] == "0.659091"
    assert custom["kind"] == "CUSTOM_QUANTITY"
    assert custom["name"] == "Custom quantity"
    assert custom["selling_option_id"] is None
    assert custom["package_count"] is None  # packages not meaningful for Custom Quantity
    assert custom["underlying_units"] == "30.000000"
    assert (custom["revenue"], custom["direct_cost"], custom["contribution"]) == (
        "55.00",
        "14.00",
        "41.00",
    )
    assert custom["contribution_margin"] == "0.745455"

    assert brownie["underlying_units"] == "15.000000"
    assert (brownie["revenue"], brownie["direct_cost"], brownie["contribution"]) == (
        "27.00",
        "9.75",
        "17.25",
    )
    assert brownie["contribution_margin"] == "0.638889"
    assert brownie["completed_orders"] == 4
    assert [o["kind"] for o in brownie["selling_options"]] == ["CUSTOM_QUANTITY"]

    assert lemonade["product_type"] == "PURCHASED"
    assert (lemonade["revenue"], lemonade["direct_cost"], lemonade["contribution"]) == (
        "12.00",
        "1.20",
        "10.80",
    )
    assert lemonade["contribution_margin"] == "0.900000"
    assert lemonade["selling_options"][0]["package_count"] == "4.000000"
    assert lemonade["completed_orders"] == 1


def test_custom_items_group_and_order_level_reconciliation(client, golden):
    body = client.get(PRODUCTS, params=P7).json()
    ci = body["custom_items"]
    assert ci == {
        "line_count": 1,
        "completed_orders": 1,
        "revenue": "5.00",
        "direct_cost": "1.50",
        "contribution": "3.50",
        "contribution_margin": "0.700000",
    }
    lvl = body["order_level"]
    assert lvl["order_adjustments"] == "-5.00"
    assert lvl["manual_tax"] == "2.00"
    assert lvl["unattributed_direct_cost"] == "0.00"
    assert lvl["unreconciled_revenue"] == "0.00"
    # Frozen reconciliation identities.
    overview = client.get(OVERVIEW, params=P7).json()["current"]
    assert body["totals"]["revenue"] == overview["revenue"]
    assert body["totals"]["direct_cost"] == overview["direct_cost"]
    assert body["totals"]["completed_orders"] == overview["completed_orders"]
    products_revenue = sum(D(r["revenue"]) for r in body["rows"])
    products_cost = sum(D(r["direct_cost"]) for r in body["rows"])
    assert products_revenue + D(ci["revenue"]) + D(lvl["order_adjustments"]) + D(
        lvl["manual_tax"]
    ) == D(overview["revenue"])
    assert products_cost + D(ci["direct_cost"]) + D(lvl["unattributed_direct_cost"]) == D(
        overview["direct_cost"]
    )


def test_most_profitable_and_highest_margin_are_distinct_metrics(client, golden):
    body = client.get(PRODUCTS, params=P7).json()
    assert body["most_profitable"] == {
        "product_id": str(golden.cookie.id),
        "product_name": "Cookie",
        "value": "90.40",
    }
    assert body["highest_margin"] == {
        "product_id": str(golden.lemonade.id),
        "product_name": "Lemonade",
        "value": "0.900000",
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"sort": "margin"}, ["Lemonade", "Cookie", "Brownie"]),
        ({"sort": "margin", "direction": "asc"}, ["Brownie", "Cookie", "Lemonade"]),
        ({"sort": "revenue"}, ["Cookie", "Brownie", "Lemonade"]),
        ({"sort": "units"}, ["Cookie", "Brownie", "Lemonade"]),
        ({"sort": "orders"}, ["Brownie", "Cookie", "Lemonade"]),
        ({"sort": "name"}, ["Brownie", "Cookie", "Lemonade"]),
        ({"sort": "name", "direction": "desc"}, ["Lemonade", "Cookie", "Brownie"]),
        ({"sort": "contribution", "direction": "asc"}, ["Lemonade", "Brownie", "Cookie"]),
    ],
)
def test_product_sorting_is_deterministic(client, golden, params, expected):
    rows = client.get(PRODUCTS, params={**P7, **params}).json()["rows"]
    assert [r["product_name"] for r in rows] == expected


def test_zero_revenue_product_has_null_margin_and_sorts_last_by_margin(client, golden, session):
    free = golden.builder.product("Freebie")
    golden.builder.order(
        completed_at=at(14, 15),
        number="FREE",
        lines=[custom_qty(free, "3", "0.00", {CostType.PACKAGING: "0.30"})],
    )
    session.commit()
    body = client.get(PRODUCTS, params={**P7, "sort": "margin"}).json()
    names = [r["product_name"] for r in body["rows"]]
    assert names[-1] == "Freebie"
    freebie = body["rows"][-1]
    assert freebie["revenue"] == "0.00"
    assert freebie["contribution"] == "-0.30"
    assert freebie["contribution_margin"] is None
    assert body["highest_margin"]["product_name"] == "Lemonade"


def test_null_line_ledger_row_is_unattributed_not_dropped_or_guessed(client, golden, session):
    o1 = golden.orders["O1"]
    make_order_cost_allocation(
        session,
        golden.builder.business,
        o1,
        order_line_id=None,
        cost_type=CostType.CUSTOM_DIRECT,
        amount=D("3.33"),
    )
    session.commit()
    products = client.get(PRODUCTS, params=P7).json()
    overview = client.get(OVERVIEW, params=P7).json()["current"]
    assert overview["direct_cost"] == "50.38"
    assert products["order_level"]["unattributed_direct_cost"] == "3.33"
    assert products["totals"]["direct_cost"] == "50.38"
    # Product costs themselves are unchanged (no inference of ownership).
    assert {r["product_name"]: r["direct_cost"] for r in products["rows"]} == {
        "Cookie": "34.60",
        "Brownie": "9.75",
        "Lemonade": "1.20",
    }


def test_ledger_row_pointing_at_another_orders_line_is_unattributed(client, golden, session):
    from sqlalchemy import select

    from app.db.models.order import OrderLine

    o1_line = session.scalars(
        select(OrderLine).where(OrderLine.order_id == golden.orders["O1"].id)
    ).first()
    make_order_cost_allocation(
        session,
        golden.builder.business,
        golden.orders["O4"],
        order_line_id=o1_line.id,  # belongs to O1, not O4
        cost_type=CostType.CUSTOM_DIRECT,
        amount=D("1.11"),
    )
    session.commit()
    products = client.get(PRODUCTS, params=P7).json()
    assert products["order_level"]["unattributed_direct_cost"] == "1.11"
    assert products["totals"]["direct_cost"] == "48.16"


def test_products_empty_period(client, golden):
    body = client.get(
        PRODUCTS, params={"range": "CUSTOM", "start_date": "2026-01-01", "end_date": "2026-01-02"}
    ).json()
    assert body["rows"] == []
    assert body["custom_items"] is None
    assert body["most_profitable"] is None and body["highest_margin"] is None
    assert body["totals"]["revenue"] == "0.00"
    assert body["totals"]["contribution_margin"] is None


# ------------------------------------------------------------------ Customers


def test_customer_summary_classification_and_ranking(client, golden):
    body = client.get(CUSTOMERS, params=P7).json()
    s = body["summary"]
    assert (s["unique_customers"], s["new_customers"], s["returning_customers"]) == (3, 2, 1)
    assert s["guest"] == {
        "completed_orders": 2,
        "revenue": "21.00",
        "average_order_value": "10.50",
        "share_of_completed_orders": "0.285714",
    }
    assert body["total"] == 3
    items = body["items"]
    assert [(i["customer_name"], i["classification"]) for i in items] == [
        ("Bob", "NEW"),
        ("Alice", "NEW"),
        ("Cara", "RETURNING"),
    ]
    bob, alice, cara = items
    assert (bob["completed_orders"], bob["revenue"], bob["average_order_value"]) == (
        2,
        "75.00",
        "37.50",
    )
    assert (alice["completed_orders"], alice["revenue"], alice["average_order_value"]) == (
        2,
        "70.00",
        "35.00",
    )
    assert (cara["completed_orders"], cara["revenue"], cara["average_order_value"]) == (
        1,
        "0.00",
        "0.00",
    )
    assert bob["customer_id"] == str(golden.bob.id)
    # Guests never appear as ranked customers.
    assert all(i["customer_name"] != "Guest" for i in items)


def test_customer_sort_by_orders_breaks_ties_by_revenue_then_name(client, golden):
    items = client.get(CUSTOMERS, params={**P7, "sort": "completed_orders"}).json()["items"]
    assert [i["customer_name"] for i in items] == ["Bob", "Alice", "Cara"]
    items = client.get(CUSTOMERS, params={**P7, "sort": "name"}).json()["items"]
    assert [i["customer_name"] for i in items] == ["Alice", "Bob", "Cara"]
    items = client.get(CUSTOMERS, params={**P7, "sort": "name", "direction": "desc"}).json()[
        "items"
    ]
    assert [i["customer_name"] for i in items] == ["Cara", "Bob", "Alice"]


def test_customer_pagination_is_total_ordered_and_keeps_summary(client, golden):
    page1 = client.get(CUSTOMERS, params={**P7, "limit": 2, "offset": 0}).json()
    page2 = client.get(CUSTOMERS, params={**P7, "limit": 2, "offset": 2}).json()
    beyond = client.get(CUSTOMERS, params={**P7, "limit": 2, "offset": 10}).json()
    assert [i["customer_name"] for i in page1["items"]] == ["Bob", "Alice"]
    assert [i["customer_name"] for i in page2["items"]] == ["Cara"]
    assert beyond["items"] == []
    for page in (page1, page2, beyond):
        assert page["total"] == 3
        assert page["summary"]["unique_customers"] == 3
        assert page["summary"]["guest"]["completed_orders"] == 2


def test_non_completed_history_never_makes_a_customer_returning_or_counted(client, golden, session):
    dan = golden.builder.customer("Dan")
    golden.builder.order(
        status=OrderStatus.CANCELED,
        canceled_at=at(1, 12),
        customer=dan,
        number="DAN-C",
        lines=[custom_qty(golden.cookie, "1", "5.00")],
    )
    golden.builder.order(
        status=OrderStatus.CONFIRMED,
        customer=dan,
        number="DAN-X",
        lines=[custom_qty(golden.cookie, "1", "5.00")],
    )
    eve = golden.builder.customer("Eve")  # only canceled/confirmed history: never listed
    golden.builder.order(
        status=OrderStatus.CANCELED,
        canceled_at=at(12, 12),
        customer=eve,
        number="EVE-C",
        lines=[custom_qty(golden.cookie, "1", "5.00")],
    )
    golden.builder.order(
        completed_at=at(13, 15),
        customer=dan,
        number="DAN-1",
        lines=[custom_qty(golden.cookie, "1", "9.00")],
    )
    session.commit()
    body = client.get(CUSTOMERS, params=P7).json()
    by_name = {i["customer_name"]: i for i in body["items"]}
    assert by_name["Dan"]["classification"] == "NEW"
    assert "Eve" not in by_name
    assert body["summary"]["unique_customers"] == 4
    assert body["summary"]["new_customers"] == 3


def test_customers_empty_period(client, golden):
    body = client.get(
        CUSTOMERS, params={"range": "CUSTOM", "start_date": "2026-01-01", "end_date": "2026-01-02"}
    ).json()
    assert body["items"] == [] and body["total"] == 0
    assert body["summary"]["unique_customers"] == 0
    assert body["summary"]["guest"]["share_of_completed_orders"] is None
    assert body["summary"]["guest"]["average_order_value"] is None


# ------------------------------------------------------------------ Orders


def _numbers(body):
    return [i["order_number"] for i in body["items"]]


def test_order_summary_canceled_largest_methods_and_outstanding(client, golden):
    body = client.get(ORDERS, params=P7).json()
    s = body["summary"]
    assert (s["completed_orders"], s["canceled_orders"]) == (7, 1)
    assert s["revenue"] == "166.00"
    assert s["average_order_value"] == "23.71"
    largest = s["largest_completed_order"]
    assert (largest["order_number"], largest["final_total"], largest["customer_name"]) == (
        "O2",
        "72.00",
        "Bob",
    )
    assert largest["completed_at"] == "2026-10-13T01:30:00Z"
    assert s["fulfillment_methods"] == [
        {"method": "PICKUP", "completed_orders": 3, "revenue": "66.00"},
        {"method": "DELIVERY", "completed_orders": 2, "revenue": "96.00"},
        {"method": "OTHER", "completed_orders": 1, "revenue": "4.00"},
        {"method": "UNSPECIFIED", "completed_orders": 1, "revenue": "0.00"},
    ]
    assert s["outstanding"] == {
        "label": "Outstanding recorded balance — as of now",
        "order_count": 4,
        "balance": "56.00",
    }
    assert body["total"] == 8
    assert _numbers(body) == ["E1", "O5", "O4", "O3", "O2", "O1", "CANC-IN", "E2"]


def test_order_rows_payment_cost_and_canceled_presentation(client, golden):
    items = {i["order_number"]: i for i in client.get(ORDERS, params=P7).json()["items"]}
    o2 = items["O2"]
    assert o2["status"] == "COMPLETED"
    assert o2["reported_at"] == "2026-10-13T01:30:00Z"
    assert o2["customer_name"] == "Bob"
    assert o2["fulfillment_method"] == "DELIVERY"
    assert (o2["final_total"], o2["payments_total"]) == ("72.00", "40.00")
    assert o2["payment_status"] == "PARTIALLY_PAID"
    assert o2["outstanding_balance"] == "32.00"
    assert (o2["direct_cost"], o2["contribution"]) == ("21.50", "50.50")

    assert items["O4"]["payment_status"] == "PAID"  # overpaid (30 vs 24)
    assert items["O4"]["outstanding_balance"] == "0.00"
    assert items["O1"]["payment_status"] == "PAID"
    assert items["O5"]["payment_status"] == "UNPAID"  # $0 Order, no payments
    assert items["O5"]["outstanding_balance"] == "0.00"
    assert (items["O5"]["direct_cost"], items["O5"]["contribution"]) == ("2.00", "-2.00")
    assert items["O3"]["customer_name"] is None and items["O3"]["customer_id"] is None
    assert items["O3"]["outstanding_balance"] == "17.00"  # no Payments => full total

    canceled = items["CANC-IN"]
    assert canceled["status"] == "CANCELED"
    assert canceled["reported_at"] == "2026-10-11T12:00:00Z"
    assert canceled["final_total"] == "77.00"
    # Canceled rows never present a balance/cost/contribution as completed financials.
    for key in ("payment_status", "outstanding_balance", "direct_cost", "contribution"):
        assert canceled[key] is None
    assert "CANC-OUT" not in items  # canceled outside the period (by canceled_at)


def test_order_filters_and_sorting(client, golden):
    only_canceled = client.get(ORDERS, params={**P7, "status": "CANCELED"}).json()
    assert _numbers(only_canceled) == ["CANC-IN"] and only_canceled["total"] == 1
    completed = client.get(ORDERS, params={**P7, "status": "COMPLETED"}).json()
    assert completed["total"] == 7 and "CANC-IN" not in _numbers(completed)
    outstanding = client.get(ORDERS, params={**P7, "outstanding_only": "true"}).json()
    assert _numbers(outstanding) == ["E1", "O3", "O2", "E2"]
    assert outstanding["total"] == 4
    both = client.get(
        ORDERS, params={**P7, "outstanding_only": "true", "status": "CANCELED"}
    ).json()
    assert both["items"] == [] and both["total"] == 0
    by_total = client.get(ORDERS, params={**P7, "sort": "final_total"}).json()
    assert _numbers(by_total)[:3] == ["CANC-IN", "O2", "O1"]
    by_balance = client.get(
        ORDERS, params={**P7, "status": "COMPLETED", "sort": "outstanding_balance"}
    ).json()
    assert _numbers(by_balance)[:4] == ["O2", "O3", "E2", "E1"]


def test_order_pagination_deterministic(client, golden):
    seen = []
    for offset in range(0, 8, 3):
        page = client.get(ORDERS, params={**P7, "limit": 3, "offset": offset}).json()
        assert page["total"] == 8
        assert page["summary"]["completed_orders"] == 7
        seen += _numbers(page)
    assert seen == ["E1", "O5", "O4", "O3", "O2", "O1", "CANC-IN", "E2"]


def test_payment_added_after_completion_changes_outstanding_not_revenue(client, golden, session):
    before = client.get(ORDERS, params=P7).json()["summary"]
    make_payment(session, golden.builder.business, golden.orders["O3"], amount=D("17.00"))
    session.commit()
    after = client.get(ORDERS, params=P7).json()["summary"]
    assert after["revenue"] == before["revenue"] == "166.00"
    assert after["outstanding"]["order_count"] == 3
    assert after["outstanding"]["balance"] == "39.00"
    overview = client.get(OVERVIEW, params=P7).json()["current"]
    assert overview["contribution"] == "118.95"


def test_orders_empty_period(client, golden):
    body = client.get(
        ORDERS, params={"range": "CUSTOM", "start_date": "2026-01-01", "end_date": "2026-01-02"}
    ).json()
    assert body["items"] == [] and body["total"] == 0
    s = body["summary"]
    assert (s["completed_orders"], s["canceled_orders"]) == (0, 0)
    assert s["largest_completed_order"] is None
    assert s["average_order_value"] is None
    assert s["outstanding"]["order_count"] == 0 and s["outstanding"]["balance"] == "0.00"
    assert [m["completed_orders"] for m in s["fulfillment_methods"]] == [0, 0, 0, 0]


def test_unspecified_method_enum_roundtrip(golden):
    assert FulfillmentMethod.PICKUP.value == "PICKUP"
    assert at(1).tzinfo is not None
