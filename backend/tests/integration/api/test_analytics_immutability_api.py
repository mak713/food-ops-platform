"""Phase 10 historical immutability (AC-ANA-009, ANA-011; Spec §13.8, §19.12): after Orders are
Completed, changing current prices, Selling Option configuration, Recipes, Ingredient costs, the
Business labor rate, Purchased costs or Surplus state must not move a single Analytics number.
A retained Product/Option rename may change only a display label. A Payment recorded later may
change the (as-of-now) outstanding balance and nothing historical."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.product import Product, SellingOption
from app.db.models.purchased_inventory import PurchasedProductInventory
from app.db.models.recipe import Recipe, RecipeRevision
from app.db.models.surplus import SurplusInventory
from tests.integration.api.analytics_support import (
    Golden,
    freeze,
    setup_golden,
)
from tests.integration.api.completion_support import (
    build_ready_order,
    produced_product,
    purchased_product,
    surplus_lot,
)
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.api.phase9_support import business_by_name
from tests.integration.schema.factories import (
    make_ingredient,
    make_payment,
    make_purchased_inventory,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)

BASE = "/api/v1/analytics"
P7 = {"range": "LAST_7_DAYS"}
D = Decimal

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


def _snapshot(client) -> dict[str, bytes]:
    out = {}
    for path, params in REQUESTS:
        response = client.get(path, params=params)
        assert response.status_code == 200, (path, response.text)
        out[path] = response.content
    return out


def _change_all_master_data(session: Session, business: Business, *, archive_products: bool):
    """Every kind of current-state change the Spec §13.8 lists, applied after Completion."""
    for option in session.scalars(select(SellingOption)):
        option.price = D("999.99")
        option.quantity_units = D("500")
        option.packaging_cost = D("77.00")
        option.is_active = False
    for product in session.scalars(select(Product)):
        product.default_packaging_cost = D("88.00")
        product.can_reuse_surplus = not product.can_reuse_surplus
        product.default_surplus_usable_days = 1
        if archive_products:
            product.is_active = False
    # Recipe: edit existing revisions and add a brand-new current revision with an ingredient.
    flour = make_ingredient(session, business, name="Flour")
    flour.weighted_average_unit_cost = D("3.000000")
    session.flush()
    for recipe in session.scalars(select(Recipe)):
        for revision in session.scalars(
            select(RecipeRevision).where(RecipeRevision.recipe_id == recipe.id)
        ):
            revision.is_current = False
            revision.yield_quantity = D("1")
            revision.active_time_minutes = 999
        new_revision = make_recipe_revision(
            session, business, recipe, revision_number=99, yield_quantity=D("2")
        )
        session.flush()
        make_recipe_revision_ingredient(session, business, new_revision, flour, quantity=D("9999"))
    # Ingredient WAC / latest / replacement costs.
    for ingredient in session.scalars(select(Ingredient)):
        ingredient.weighted_average_unit_cost = D("55.000000")
        ingredient.latest_purchase_unit_cost = D("66.000000")
        ingredient.replacement_unit_cost = D("77.000000")
    # Business labor rate.
    business.default_labor_rate = D("250.00")
    # Purchased current / replacement costs.
    lemonade = session.scalars(select(Product).where(Product.name.like("Lemonade%"))).first()
    if lemonade is not None:
        inventory = session.scalars(
            select(PurchasedProductInventory).where(
                PurchasedProductInventory.product_id == lemonade.id
            )
        ).first() or make_purchased_inventory(session, business, lemonade)
        inventory.weighted_average_unit_cost = D("40.000000")
        inventory.latest_purchase_unit_cost = D("41.000000")
        inventory.replacement_unit_cost = D("42.000000")
    # Current Surplus physical state.
    for lot in session.scalars(select(SurplusInventory)):
        lot.physical_quantity = D("0")
        lot.unit_cost_basis = D("123.000000")
        lot.is_reusable = False
    session.commit()


def test_all_numbers_survive_every_current_master_data_change(client, session, monkeypatch):
    golden: Golden = setup_golden(client, session, monkeypatch)
    business = golden.builder.business
    before = _snapshot(client)

    _change_all_master_data(session, business, archive_products=False)

    assert _snapshot(client) == before  # byte-for-byte: JSON and CSV


def test_archiving_a_product_never_changes_a_number(client, session, monkeypatch):
    golden = setup_golden(client, session, monkeypatch)
    before = client.get(f"{BASE}/products", params=P7).json()
    _change_all_master_data(session, golden.builder.business, archive_products=True)
    after = client.get(f"{BASE}/products", params=P7).json()

    def numbers(body):
        return (
            [{k: v for k, v in row.items() if k not in ("is_active",)} for row in body["rows"]],
            body["totals"],
            body["order_level"],
            body["custom_items"],
        )

    assert numbers(after) == numbers(before)
    assert all(row["is_active"] is False for row in after["rows"])  # flag is a label only


def test_renaming_retained_product_and_option_changes_labels_only(client, session, monkeypatch):
    golden = setup_golden(client, session, monkeypatch)
    before = client.get(f"{BASE}/products", params=P7).json()

    golden.cookie.name = "Cookie Deluxe"
    golden.six_pack.name = "Half-dozen"
    session.commit()

    after = client.get(f"{BASE}/products", params=P7).json()
    cookie_before = next(r for r in before["rows"] if r["product_id"] == str(golden.cookie.id))
    cookie_after = next(r for r in after["rows"] if r["product_id"] == str(golden.cookie.id))
    assert cookie_after["product_name"] == "Cookie Deluxe"
    assert [o["name"] for o in cookie_after["selling_options"]][0] == "Half-dozen"

    def strip_labels(row):
        row = {k: v for k, v in row.items() if k != "product_name"}
        row["selling_options"] = [
            {k: v for k, v in o.items() if k != "name"} for o in row["selling_options"]
        ]
        return row

    assert strip_labels(cookie_after) == strip_labels(cookie_before)
    assert after["totals"] == before["totals"]
    assert after["order_level"] == before["order_level"]
    assert after["most_profitable"]["value"] == before["most_profitable"]["value"]


def test_a_later_payment_changes_only_the_as_of_now_outstanding_balance(
    client, session, monkeypatch
):
    golden = setup_golden(client, session, monkeypatch)
    overview_before = client.get(f"{BASE}/overview", params=P7).content
    products_before = client.get(f"{BASE}/products", params=P7).content
    trends_before = client.get(f"{BASE}/trends", params=P7).content
    orders_before = client.get(f"{BASE}/orders", params=P7).json()

    make_payment(session, golden.builder.business, golden.orders["O2"], amount=D("32.00"))
    session.commit()

    assert client.get(f"{BASE}/overview", params=P7).content == overview_before
    assert client.get(f"{BASE}/products", params=P7).content == products_before
    assert client.get(f"{BASE}/trends", params=P7).content == trends_before
    orders_after = client.get(f"{BASE}/orders", params=P7).json()
    assert orders_before["summary"]["outstanding"]["balance"] == "56.00"
    assert orders_after["summary"]["outstanding"]["balance"] == "24.00"
    assert orders_after["summary"]["revenue"] == orders_before["summary"]["revenue"]
    o2 = next(i for i in orders_after["items"] if i["order_number"] == "O2")
    assert (o2["payment_status"], o2["outstanding_balance"]) == ("PAID", "0.00")


# ------------------------------------------------------------------ via the real Completion flow


def test_real_completion_ledger_numbers_survive_master_data_changes(client, session, monkeypatch):
    """Purchased, Surplus, Packaging and Custom Direct rows are written by the real
    `POST /orders/{id}/complete` transaction (no hand-inserted ledger) and then every current
    cost / price / recipe / labor / surplus setting is changed."""
    signup(client, email=f"ana-real-{uuid.uuid4().hex[:8]}@example.com")
    business = business_by_name(session)
    juice = purchased_product(session, business, physical="10", wac="1.50")
    cookie = produced_product(session, business)
    surplus_lot(session, business, cookie, physical="6", basis="0.50")
    order = build_ready_order(
        session,
        business,
        [
            {"product": juice, "qty": "4", "packaging": "0.40"},
            {"product": cookie, "qty": "6", "packaging": "0.25"},
            {"product": None, "qty": "1", "custom_estimate": "2.50"},
        ],
    )
    for line in order.lines:  # make Order totals exactly the sum of its line snapshots
        line.line_subtotal = D("10.00")
    order.subtotal = D("30.00")
    order.final_total = D("30.00")
    session.commit()

    response = client.post(
        f"/api/v1/orders/{order.id}/complete",
        json={"version": order.version},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text

    # Completion stamped `completed_at` with the real clock; pin the *report* clock just after
    # it so `generated_at` is identical across the two snapshots (it is the only moving field).
    freeze(monkeypatch, datetime.now(UTC) + timedelta(minutes=1))

    def real_snapshot():
        return {
            p: client.get(p, params=pr).content
            for p, pr in [
                (f"{BASE}/overview", {"range": "LAST_7_DAYS"}),
                (f"{BASE}/products", {"range": "LAST_7_DAYS"}),
                (f"{BASE}/orders", {"range": "LAST_7_DAYS"}),
                (f"{BASE}/trends.csv", {"range": "LAST_7_DAYS"}),
            ]
        }

    before = real_snapshot()
    overview = client.get(f"{BASE}/overview", params={"range": "LAST_7_DAYS"}).json()["current"]
    # Revenue 30.00; Direct Cost = PURCHASED 6.00 + SURPLUS 3.00 + PACKAGING 0.65 + CUSTOM 2.50.
    assert overview["revenue"] == "30.00"
    assert overview["direct_cost"] == "12.15"
    assert overview["contribution"] == "17.85"
    assert overview["completed_orders"] == 1
    products = client.get(f"{BASE}/products", params={"range": "LAST_7_DAYS"}).json()
    assert products["custom_items"]["direct_cost"] == "2.50"
    assert products["order_level"]["unreconciled_revenue"] == "0.00"
    assert products["order_level"]["unattributed_direct_cost"] == "0.00"

    _change_all_master_data(session, business, archive_products=False)
    assert real_snapshot() == before
