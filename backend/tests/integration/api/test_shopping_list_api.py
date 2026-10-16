"""Phase 9 Shopping List API tests (Acceptance Matrix S2–S8): presets, custom range
validation, shortage-first ordering, drilldown, incomplete-recipe notice, restock
reflected on the next read, and exclusion of Drafts/Canceled demand."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.enums import OrderStatus
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.api.phase9_support import (
    business_by_name,
    business_today,
    confirm_demand,
    make_recipe_product,
    make_stocked_ingredient,
)
from tests.integration.schema.factories import make_customer, make_product

URL = "/api/v1/shopping-list"


def _setup(client, session: Session):
    signup(client)
    business = business_by_name(session)
    return business, business_today(business)


def test_default_range_uses_business_horizon_and_presets_override(client, session: Session):
    business, today = _setup(client, session)
    business.shopping_horizon_days = 3
    session.commit()

    body = client.get(URL).json()
    assert body["range"]["horizon_days"] == 3 and body["range"]["is_custom"] is False
    assert body["range"]["start_date"] == today.isoformat()
    assert body["range"]["end_date"] == (today + timedelta(days=3)).isoformat()

    body7 = client.get(f"{URL}?horizon_days=7").json()
    assert body7["range"]["horizon_days"] == 7
    assert body7["range"]["end_date"] == (today + timedelta(days=7)).isoformat()


def test_invalid_preset_and_mixed_params_return_structured_422(client, session: Session):
    _business, today = _setup(client, session)

    response = client.get(f"{URL}?horizon_days=5")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "SHOPPING_LIST_RANGE_INVALID"
    assert error["issues"][0]["code"] == "SHOPPING_LIST_HORIZON_INVALID"
    assert error["issues"][0]["severity"] == "ERROR"

    mixed = client.get(
        f"{URL}?horizon_days=3&start_date={today.isoformat()}&end_date={today.isoformat()}"
    )
    assert mixed.status_code == 422
    assert mixed.json()["error"]["issues"][0]["code"] == "SHOPPING_LIST_RANGE_CONFLICT"


def test_custom_range_validation_rules(client, session: Session):
    _business, today = _setup(client, session)

    past = client.get(
        f"{URL}?start_date={(today - timedelta(days=1)).isoformat()}&end_date={today.isoformat()}"
    )
    assert past.status_code == 422
    assert past.json()["error"]["issues"][0]["code"] == "SHOPPING_LIST_RANGE_START_IN_PAST"

    reversed_range = client.get(
        f"{URL}?start_date={(today + timedelta(days=3)).isoformat()}"
        f"&end_date={(today + timedelta(days=2)).isoformat()}"
    )
    assert reversed_range.status_code == 422
    assert (
        reversed_range.json()["error"]["issues"][0]["code"]
        == "SHOPPING_LIST_RANGE_END_BEFORE_START"
    )

    too_long = client.get(
        f"{URL}?start_date={today.isoformat()}&end_date={(today + timedelta(days=90)).isoformat()}"
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["issues"][0]["code"] == "SHOPPING_LIST_RANGE_TOO_LONG"

    exactly_90 = client.get(
        f"{URL}?start_date={today.isoformat()}&end_date={(today + timedelta(days=89)).isoformat()}"
    )
    assert exactly_90.status_code == 200
    assert exactly_90.json()["range"]["is_custom"] is True

    half = client.get(f"{URL}?start_date={today.isoformat()}")
    assert half.status_code == 422
    assert half.json()["error"]["issues"][0]["code"] == "SHOPPING_LIST_RANGE_INCOMPLETE"


def test_unauthenticated_request_is_rejected(client, session: Session):
    assert client.get(URL).status_code == 401


def test_empty_state_has_no_shortages_or_covered(client, session: Session):
    _setup(client, session)
    body = client.get(URL).json()
    assert body["shortages"] == [] and body["covered"] == [] and body["incomplete_recipe"] == []


def test_worked_example_shortage_is_reserved_minus_physical_with_drilldown(
    client, session: Session
):
    """Flour 100 on hand; 60 g needed today and 60 g in two days -> cumulative shortage
    0 today and 20 at day 2 (ADR-130), with the drilldown naming the requirement and the
    Order that feeds it."""
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, name="Flour", physical="100")
    cookie = make_recipe_product(
        session,
        business,
        name="Cookie",
        yield_quantity=Decimal("12"),
        ingredients=[(flour, Decimal("60"))],
    )
    customer = make_customer(session, business, name="Alice")
    session.flush()
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today, customer_id=customer.id
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today + timedelta(days=2)
    )

    body = client.get(f"{URL}?horizon_days=7").json()
    assert len(body["shortages"]) == 1
    item = body["shortages"][0]
    assert item["ingredient_name"] == "Flour" and item["canonical_unit"] == "g"
    assert Decimal(item["physical_quantity"]) == Decimal("100")
    assert Decimal(item["reserved_through_end"]) == Decimal("120")
    assert Decimal(item["shortage_quantity"]) == Decimal("20")
    assert item["needed_by_date"] == (today + timedelta(days=2)).isoformat()

    by_date = {e["demand_date"]: e for e in item["by_date"]}
    assert Decimal(by_date[today.isoformat()]["cumulative_shortage_through_date"]) == 0
    assert (
        Decimal(
            by_date[(today + timedelta(days=2)).isoformat()]["cumulative_shortage_through_date"]
        )
        == 20
    )

    assert len(item["contributors"]) == 2
    first = item["contributors"][0]
    assert first["demand_date"] == today.isoformat()
    assert first["product_name"] == "Cookie"
    assert Decimal(first["reserved_quantity"]) == Decimal("60")
    assert first["is_overdue"] is False and first["is_in_production"] is False
    assert first["orders"][0]["customer_name"] == "Alice"
    assert Decimal(first["orders"][0]["demand_quantity"]) == Decimal("12")
    assert body["covered"] == []


def test_overdue_reservation_counts_and_is_flagged_overdue(client, session: Session):
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="100")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("40"))]
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today - timedelta(days=12)
    )
    confirm_demand(
        session, business, cookie, quantity="24", fulfillment_date=today + timedelta(days=1)
    )

    item = client.get(f"{URL}?horizon_days=3").json()["shortages"][0]
    # 40 (overdue) + 80 (tomorrow) = 120 - 100 = 20
    assert Decimal(item["shortage_quantity"]) == Decimal("20")
    overdue = [c for c in item["contributors"] if c["is_overdue"]]
    assert len(overdue) == 1


def test_reservation_beyond_range_never_inflates_and_is_not_listed(client, session: Session):
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="10")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("5"))]
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today + timedelta(days=1)
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today + timedelta(days=20)
    )

    body = client.get(f"{URL}?horizon_days=3").json()
    assert body["shortages"] == []
    assert len(body["covered"]) == 1
    covered = body["covered"][0]
    assert Decimal(covered["reserved_through_end"]) == Decimal("5")
    assert all(
        c["demand_date"] <= (today + timedelta(days=3)).isoformat() for c in covered["contributors"]
    )


def test_ingredient_reserved_only_beyond_range_is_omitted(client, session: Session):
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="1")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("500"))]
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today + timedelta(days=30)
    )
    body = client.get(f"{URL}?horizon_days=7").json()
    assert body["shortages"] == [] and body["covered"] == []

    custom = client.get(
        f"{URL}?start_date={(today + timedelta(days=28)).isoformat()}"
        f"&end_date={(today + timedelta(days=32)).isoformat()}"
    ).json()
    assert len(custom["shortages"]) == 1


def test_future_start_custom_range_includes_pre_range_demand_in_cumulative(
    client, session: Session
):
    """A range starting next week still subtracts physical stock against *all* earlier
    demand (cumulative semantics) — stock can't be double-spent across the gap."""
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="100")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("80"))]
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today + timedelta(days=1)
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today + timedelta(days=9)
    )
    body = client.get(
        f"{URL}?start_date={(today + timedelta(days=8)).isoformat()}"
        f"&end_date={(today + timedelta(days=10)).isoformat()}"
    ).json()
    item = body["shortages"][0]
    assert Decimal(item["shortage_quantity"]) == Decimal("60")  # 160 reserved - 100 physical
    assert len(item["contributors"]) == 2  # includes the pre-range demand


def test_shortage_first_ordering_then_covered_group(client, session: Session):
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, name="Flour", physical="10")
    sugar = make_stocked_ingredient(session, business, name="Sugar", physical="10")
    salt = make_stocked_ingredient(session, business, name="Salt", physical="500")
    cookie = make_recipe_product(
        session,
        business,
        yield_quantity=Decimal("12"),
        ingredients=[(flour, Decimal("50")), (sugar, Decimal("200")), (salt, Decimal("5"))],
    )
    confirm_demand(session, business, cookie, quantity="12", fulfillment_date=today)

    body = client.get(URL).json()
    # Sugar short by 190, Flour short by 40 -> larger shortage first; Salt is covered.
    assert [i["ingredient_name"] for i in body["shortages"]] == ["Sugar", "Flour"]
    assert [i["ingredient_name"] for i in body["covered"]] == ["Salt"]


def test_incomplete_recipe_demand_is_a_notice_and_invents_no_ingredients(client, session: Session):
    business, today = _setup(client, session)
    norecipe = make_product(session, business, name="Mystery Loaf")
    session.flush()
    confirm_demand(session, business, norecipe, quantity="6", fulfillment_date=today)

    body = client.get(URL).json()
    assert body["shortages"] == [] and body["covered"] == []
    assert len(body["incomplete_recipe"]) == 1
    notice = body["incomplete_recipe"][0]
    assert notice["product_name"] == "Mystery Loaf"
    assert Decimal(notice["demand_quantity"]) == Decimal("6")


def test_restock_is_reflected_on_the_next_read_without_any_recalculation(client, session: Session):
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="10")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("100"))]
    )
    confirm_demand(session, business, cookie, quantity="12", fulfillment_date=today)

    before = client.get(URL).json()
    assert Decimal(before["shortages"][0]["shortage_quantity"]) == Decimal("90")

    # Real restock through the real endpoint (physical 10 -> 10 + 500).
    response = client.post(
        f"/api/v1/ingredients/{flour.id}/inventory/restock",
        json={
            "version": flour.version,
            "quantity": "500",
            "unit": "g",
            "unit_cost": "0.02",
        },
        headers=csrf_headers(client),
    )
    assert response.status_code in (200, 201), response.text

    after = client.get(URL).json()
    assert after["shortages"] == []
    assert Decimal(after["covered"][0]["physical_quantity"]) == Decimal("510")


def test_draft_and_canceled_orders_contribute_nothing(client, session: Session):
    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="1")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("100"))]
    )
    confirm_demand(
        session, business, cookie, quantity="12", fulfillment_date=today, status=OrderStatus.DRAFT
    )
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=today,
        status=OrderStatus.CANCELED,
    )
    body = client.get(URL).json()
    assert body["shortages"] == [] and body["covered"] == []


def test_purchased_product_demand_never_appears(client, session: Session):
    from app.db.enums import ProductType

    business, today = _setup(client, session)
    resale = make_product(session, business, product_type=ProductType.PURCHASED, name="Jam")
    session.flush()
    confirm_demand(session, business, resale, quantity="3", fulfillment_date=today)
    body = client.get(URL).json()
    assert body["shortages"] == [] and body["covered"] == [] and body["incomplete_recipe"] == []


def test_in_production_requirement_is_labeled_and_still_counts(client, session: Session):
    from sqlalchemy import select

    from app.db.models.production import ProductionRequirement

    business, today = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="10")
    cookie = make_recipe_product(
        session, business, yield_quantity=Decimal("12"), ingredients=[(flour, Decimal("100"))]
    )
    confirm_demand(session, business, cookie, quantity="12", fulfillment_date=today)
    requirement = session.scalars(select(ProductionRequirement)).one()

    start = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert start.status_code == 201, start.text

    item = client.get(URL).json()["shortages"][0]
    assert Decimal(item["shortage_quantity"]) == Decimal("90")
    assert item["contributors"][0]["is_in_production"] is True
