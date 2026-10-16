"""Phase 9 approved Q3 work: derived Reserved/Available (Ingredient), Allocated/Available
(Surplus), and Production Plan shortage/affected-demand visibility — every figure read
through the same authoritative derivation the Shopping List uses (Spec §7.3, §12.11,
§12.13, §20.13)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from tests.integration.api.helpers import signup
from tests.integration.api.phase9_support import (
    business_by_name,
    business_today,
    confirm_demand,
    make_recipe_product,
    make_stocked_ingredient,
)
from tests.integration.schema.factories import (
    make_customer,
    make_product,
    make_surplus_inventory,
)


def _setup(client, session: Session):
    signup(client)
    business = business_by_name(session)
    return business, business_today(business)


def _shortage_world(session, business, today, *, physical="100", per_batch="60"):
    flour = make_stocked_ingredient(session, business, name="Flour", physical=physical)
    cookie = make_recipe_product(
        session,
        business,
        name="Cookie",
        yield_quantity=Decimal("12"),
        ingredients=[(flour, Decimal(per_batch))],
    )
    customer = make_customer(session, business, name="Alice")
    session.flush()
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=today,
        customer_id=customer.id,
        order_number="ORD-A",
    )
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=today + timedelta(days=2),
        order_number="ORD-B",
    )
    return flour, cookie


# ---------------------------------------------------------------- Ingredient Reserved/Available


def test_ingredient_detail_and_list_expose_derived_reserved_and_available(client, session: Session):
    business, today = _setup(client, session)
    flour, _cookie = _shortage_world(session, business, today)  # reserved 120, physical 100

    detail = client.get(f"/api/v1/ingredients/{flour.id}").json()
    assert Decimal(detail["physical_quantity"]) == Decimal("100")
    assert Decimal(detail["reserved_quantity"]) == Decimal("120")
    assert Decimal(detail["available_quantity"]) == Decimal("-20")  # negative is shown as-is

    listing = client.get("/api/v1/ingredients").json()["items"]
    row = next(i for i in listing if i["id"] == str(flour.id))
    assert Decimal(row["reserved_quantity"]) == Decimal("120")
    assert Decimal(row["available_quantity"]) == Decimal("-20")


def test_ingredient_without_reservations_has_zero_reserved(client, session: Session):
    business, _today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="50")
    session.commit()
    detail = client.get(f"/api/v1/ingredients/{flour.id}").json()
    assert Decimal(detail["reserved_quantity"]) == 0
    assert Decimal(detail["available_quantity"]) == Decimal("50")


def test_ingredient_reserved_is_derived_not_editable(client, session: Session):
    from tests.integration.api.helpers import csrf_headers

    business, _today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="50")
    session.commit()
    response = client.patch(
        f"/api/v1/ingredients/{flour.id}",
        json={"version": flour.version, "reserved_quantity": "999"},
        headers=csrf_headers(client),
    )
    # Unknown field is ignored by the PATCH schema; the derived value is untouched.
    assert response.status_code == 200
    assert Decimal(response.json()["reserved_quantity"]) == 0


def test_reserved_ignores_other_tenants_reservations(client, session: Session):
    from tests.integration.schema.factories import make_business_graph

    business, today = _setup(client, session)
    flour, _ = _shortage_world(session, business, today)

    other = make_business_graph(session, name="Other Business")
    other_flour = make_stocked_ingredient(session, other, name="Flour", physical="5")
    other_cookie = make_recipe_product(
        session, other, ingredients=[(other_flour, Decimal("900"))], yield_quantity=Decimal("12")
    )
    confirm_demand(session, other, other_cookie, quantity="12", fulfillment_date=today)

    detail = client.get(f"/api/v1/ingredients/{flour.id}").json()
    assert Decimal(detail["reserved_quantity"]) == Decimal("120")


# --------------------------------------------------------------------- Surplus Allocated/Available


def test_surplus_exposes_distinct_physical_allocated_available(client, session: Session):
    """Spec §20.14: physical 6, allocated 4, available 2."""
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business)
    cookie = make_recipe_product(
        session, business, ingredients=[(flour, Decimal("10"))], yield_quantity=Decimal("12")
    )
    lot = make_surplus_inventory(session, business, cookie, physical_quantity=6)
    session.flush()
    # Real recalculation: 4 units of demand are covered by eligible surplus first.
    confirm_demand(
        session, business, cookie, quantity="4", fulfillment_date=today + timedelta(days=1)
    )

    detail = client.get(f"/api/v1/surplus/{lot.id}").json()
    assert Decimal(detail["physical_quantity"]) == Decimal("6")
    assert Decimal(detail["allocated_quantity"]) == Decimal("4")
    assert Decimal(detail["available_quantity"]) == Decimal("2")

    row = next(i for i in client.get("/api/v1/surplus").json()["items"] if i["id"] == str(lot.id))
    assert Decimal(row["allocated_quantity"]) == Decimal("4")
    assert Decimal(row["available_quantity"]) == Decimal("2")


def test_surplus_with_no_allocations_is_fully_available(client, session: Session):
    business, _today = _setup(client, session)
    cookie = make_product(session, business, name="Cookie")
    lot = make_surplus_inventory(session, business, cookie, physical_quantity=6)
    session.commit()
    detail = client.get(f"/api/v1/surplus/{lot.id}").json()
    assert Decimal(detail["allocated_quantity"]) == 0
    assert Decimal(detail["available_quantity"]) == Decimal("6")


# -------------------------------------------------------------------- Production Plan visibility


def test_production_plan_shows_shortage_and_affected_orders(client, session: Session):
    business, today = _setup(client, session)
    flour, _cookie = _shortage_world(session, business, today)

    items = client.get("/api/v1/production-runs/requirements").json()["items"]
    assert len(items) == 2
    today_req = next(i for i in items if i["demand_date"] == today.isoformat())
    later_req = next(
        i for i in items if i["demand_date"] == (today + timedelta(days=2)).isoformat()
    )

    # As of today only 60 of 100 is reserved -> no shortage; as of day+2 cumulative 120 -> 20.
    assert today_req["has_shortage"] is False
    assert later_req["has_shortage"] is True
    need = later_req["ingredient_needs"][0]
    assert need["ingredient_name"] == "Flour" and need["canonical_unit"] == "g"
    assert Decimal(need["required_quantity"]) == Decimal("60")
    assert Decimal(need["shortage_quantity"]) == Decimal("20")

    assert [o["order_number"] for o in today_req["affected_orders"]] == ["ORD-A"]
    assert today_req["affected_orders"][0]["customer_name"] == "Alice"
    assert Decimal(today_req["affected_orders"][0]["demand_quantity"]) == Decimal("12")

    single = client.get(f"/api/v1/production-runs/requirements/{later_req['id']}").json()
    assert single["has_shortage"] is True


def test_same_shortage_figure_on_production_plan_shopping_list_and_ingredient(
    client, session: Session
):
    """Spec §20.13 — one derivation, three screens, identical numbers."""
    business, today = _setup(client, session)
    flour, _cookie = _shortage_world(session, business, today)

    shopping = client.get("/api/v1/shopping-list?horizon_days=7").json()["shortages"][0]
    plan = client.get("/api/v1/production-runs/requirements").json()["items"]
    plan_need = max(
        (n for r in plan for n in r["ingredient_needs"]),
        key=lambda n: Decimal(n["shortage_quantity"]),
    )
    ingredient = client.get(f"/api/v1/ingredients/{flour.id}").json()

    assert Decimal(shopping["shortage_quantity"]) == Decimal(plan_need["shortage_quantity"])
    assert Decimal(shopping["shortage_quantity"]) == -Decimal(ingredient["available_quantity"])


def test_production_plan_incomplete_fields_default_safely_for_started_requirement(
    client, session: Session
):
    """A requirement with no ingredient rows (e.g. incomplete recipe) still serializes."""
    business, today = _setup(client, session)
    norecipe = make_product(session, business, name="Loaf")
    session.flush()
    confirm_demand(session, business, norecipe, quantity="6", fulfillment_date=today)
    # INCOMPLETE_RECIPE requirements are not listed as actionable; fetch directly.
    from sqlalchemy import select

    from app.db.models.production import ProductionRequirement

    requirement = session.scalars(select(ProductionRequirement)).one()
    body = client.get(f"/api/v1/production-runs/requirements/{requirement.id}").json()
    assert body["has_shortage"] is False
    assert body["ingredient_needs"] == []
    assert [o["order_number"] for o in body["affected_orders"]] != []
