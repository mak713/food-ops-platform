"""Phase 9 tenant isolation (Spec §19.14 names "Dashboard data" explicitly; AC-SEC-005;
release-blocking): the Dashboard and Shopping List never expose, count, or are influenced
by another Business's data.

The strongest available proof is used: build Business A's world, capture A's responses,
then add a *much larger, overlapping-shape* Business B world and prove A's responses are
**byte-for-byte unchanged**; and B's responses never contain any of A's identifiers.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.api.v1 import dashboard as dashboard_router
from app.db.enums import OrderStatus, ProductType
from tests.integration.api.helpers import signup
from tests.integration.api.phase9_support import (
    business_by_name,
    confirm_demand,
    make_recipe_product,
    make_stocked_ingredient,
)
from tests.integration.schema.factories import (
    make_ingredient,
    make_product,
    make_surplus_inventory,
)

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
TODAY = date(2026, 6, 10)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(dashboard_router, "_utc_now", lambda: NOW)


def _signup_two(client):
    signup(client, email="iso-a@example.com", business_name="Bakery A")
    a = {"session": client.cookies.get("fo_session"), "csrf": client.cookies.get("fo_csrf")}
    client.cookies.clear()
    signup(client, email="iso-b@example.com", business_name="Bakery B")
    b = {"session": client.cookies.get("fo_session"), "csrf": client.cookies.get("fo_csrf")}
    return a, b


def _use(client, tenant: dict) -> None:
    client.cookies.clear()
    client.cookies.set("fo_session", tenant["session"])
    client.cookies.set("fo_csrf", tenant["csrf"])


def _build_world(session, business, *, scale: int, tag: str):
    """Every alert/section kind, with names that deliberately overlap across tenants
    (same ingredient/product names) plus a tag-unique marker order number."""
    flour = make_stocked_ingredient(session, business, name="Flour", physical="10")
    butter = make_ingredient(session, business, name="Butter")
    butter.physical_quantity = Decimal("-5")
    cookie = make_recipe_product(
        session,
        business,
        name="Cookie",
        ingredients=[(flour, Decimal(100 * scale))],
        elapsed_time_minutes=120,
    )
    jam = make_product(session, business, product_type=ProductType.PURCHASED, name="Jam")
    loaf = make_product(session, business, name="Mystery Loaf")
    lot = make_surplus_inventory(session, business, cookie, physical_quantity=6)
    lot.usable_through_date = TODAY + timedelta(days=1)
    session.flush()
    for i in range(scale):
        confirm_demand(
            session,
            business,
            cookie,
            quantity="12",
            fulfillment_date=TODAY,
            fulfillment_time=time(8, 30),
            today=TODAY,
            order_number=f"ORD-{tag}-{i}",
        )
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=TODAY - timedelta(days=2),
        today=TODAY,
        order_number=f"ORD-{tag}-LATE",
    )
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=TODAY + timedelta(days=2),
        today=TODAY,
        order_number=f"ORD-{tag}-NOTIME",
    )
    confirm_demand(
        session,
        business,
        jam,
        quantity="4",
        fulfillment_date=TODAY,
        today=TODAY,
        order_number=f"ORD-{tag}-JAM",
        status=OrderStatus.CONFIRMED,
    )
    confirm_demand(
        session,
        business,
        loaf,
        quantity="3",
        fulfillment_date=TODAY,
        today=TODAY,
        order_number=f"ORD-{tag}-LOAF",
    )
    return {
        "ingredients": [flour, butter],
        "products": [cookie, jam, loaf],
        "lot": lot,
    }


def _ids(session_objects) -> set[str]:
    ids: set[str] = set()
    for obj in session_objects:
        ids.add(str(obj.id))
    return ids


def test_adding_another_tenants_data_never_changes_a_tenants_responses(client, session):
    a_tenant, b_tenant = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    _build_world(session, biz_a, scale=2, tag="A")

    _use(client, a_tenant)
    dash_before = client.get("/api/v1/dashboard").json()
    shop_before = client.get("/api/v1/shopping-list?horizon_days=7").json()
    plan_before = client.get("/api/v1/production-runs/requirements").json()
    assert dash_before["attention"], "Business A's world must produce real alerts"

    _build_world(session, biz_b, scale=9, tag="B")  # far larger, overlapping shapes

    _use(client, a_tenant)
    assert client.get("/api/v1/dashboard").json() == dash_before
    assert client.get("/api/v1/shopping-list?horizon_days=7").json() == shop_before
    assert client.get("/api/v1/production-runs/requirements").json() == plan_before


def test_responses_contain_no_foreign_identifiers_in_either_direction(client, session):
    a_tenant, b_tenant = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    world_a = _build_world(session, biz_a, scale=2, tag="A")
    world_b = _build_world(session, biz_b, scale=3, tag="B")

    def blob(tenant) -> str:
        _use(client, tenant)
        return json.dumps(
            [
                client.get("/api/v1/dashboard").json(),
                client.get("/api/v1/shopping-list?horizon_days=7").json(),
                client.get("/api/v1/production-runs/requirements").json(),
            ]
        )

    a_text, b_text = blob(a_tenant), blob(b_tenant)

    a_ids = _ids(world_a["ingredients"] + world_a["products"] + [world_a["lot"]])
    b_ids = _ids(world_b["ingredients"] + world_b["products"] + [world_b["lot"]])
    assert not any(i in a_text for i in b_ids)
    assert not any(i in b_text for i in a_ids)
    assert "ORD-B-" not in a_text and "ORD-A-" not in b_text
    # And every UUID in A's payload is one of A's own rows.
    for found in set(UUID_RE.findall(a_text)):
        assert found not in b_ids


def test_client_supplied_business_id_param_and_header_are_ignored(client, session):
    a_tenant, b_tenant = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    _build_world(session, biz_a, scale=1, tag="A")
    _build_world(session, biz_b, scale=4, tag="B")

    _use(client, a_tenant)
    baseline_dash = client.get("/api/v1/dashboard").json()
    baseline_shop = client.get("/api/v1/shopping-list?horizon_days=7").json()

    forged = {"X-Business-Id": str(biz_b.id)}
    assert client.get(f"/api/v1/dashboard?business_id={biz_b.id}", headers=forged).json() == (
        baseline_dash
    )
    assert (
        client.get(
            f"/api/v1/shopping-list?horizon_days=7&business_id={biz_b.id}", headers=forged
        ).json()
        == baseline_shop
    )


def test_foreign_data_never_inflates_counts_or_shortages(client, session):
    a_tenant, b_tenant = _signup_two(client)
    biz_a = business_by_name(session, "Bakery A")
    biz_b = business_by_name(session, "Bakery B")
    # A has plenty of flour and a tiny order -> no shortage. B is hugely short.
    flour_a = make_stocked_ingredient(session, biz_a, name="Flour", physical="100000")
    cookie_a = make_recipe_product(
        session, biz_a, name="Cookie", ingredients=[(flour_a, Decimal("10"))]
    )
    confirm_demand(session, biz_a, cookie_a, quantity="12", fulfillment_date=TODAY, today=TODAY)
    _build_world(session, biz_b, scale=9, tag="B")

    _use(client, a_tenant)
    body = client.get("/api/v1/dashboard").json()
    assert not any(a["code"] == "INGREDIENT_SHORTAGE" for a in body["attention"])
    assert client.get("/api/v1/shopping-list?horizon_days=7").json()["shortages"] == []
    assert body["todays_snapshot"]["orders_due_today"] == 1
    # Sanity: B really is in trouble when viewed as B.
    _use(client, b_tenant)
    assert any(
        a["code"] == "INGREDIENT_SHORTAGE"
        for a in client.get("/api/v1/dashboard").json()["attention"]
    )
