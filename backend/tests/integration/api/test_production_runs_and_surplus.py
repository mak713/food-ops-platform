"""API-level smoke tests for the Phase 8 Production Run and Surplus HTTP endpoints —
verifies routing/request/response wiring; the underlying business logic is already
covered exhaustively at the service layer (tests/integration/services/)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.services.operational_recalculation_service import recalculate_product_closure
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import (
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)

_TODAY = date(2026, 6, 1)


def _business(session: Session) -> Business:
    return session.scalars(select(Business).where(Business.name == "Test Bakery")).one()


def _produced_setup(client, session: Session, *, demand_quantity=Decimal("6")):
    signup(client)
    business = _business(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("1000")
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=demand_quantity,
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()
    return business, product, order


def test_start_and_finish_production_via_http(client, session: Session):
    from app.db.models.production import ProductionRequirement

    _business_obj, product, order = _produced_setup(client, session)
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()

    start_response = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert start_response.status_code == 201, start_response.text
    run = start_response.json()
    assert run["status"] == "IN_PRODUCTION"
    assert run["planned_batches"] == 1

    get_response = client.get(f"/api/v1/production-runs/{run['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == run["id"]

    finish_response = client.post(
        f"/api/v1/production-runs/{run['id']}/finish",
        json={"version": run["version"], "actual_output_quantity": "6"},
        headers=csrf_headers(client),
    )
    assert finish_response.status_code == 200, finish_response.text
    finished = finish_response.json()
    assert finished["run"]["status"] == "COMPLETED"
    assert finished["is_shortfall"] is False
    assert finished["negative_inventory_ingredient_ids"] == []

    order_response = client.get(f"/api/v1/orders/{order.id}")
    assert order_response.status_code == 200
    assert order_response.json()["status"] == "READY"


def test_list_production_requirements_via_http(client, session: Session):
    _business_obj, product, _order = _produced_setup(client, session)

    response = client.get("/api/v1/production-runs/requirements")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["product_id"] == str(product.id)
    assert body["items"][0]["product_name"] == product.name
    assert body["items"][0]["production_demand_quantity"] == "6.000000"


def test_get_single_production_requirement_via_http(client, session: Session):
    from app.db.models.production import ProductionRequirement

    _business_obj, product, _order = _produced_setup(client, session)
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()

    response = client.get(f"/api/v1/production-runs/requirements/{requirement.id}")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(requirement.id)


def test_list_production_runs_invalid_status_returns_structured_422(client, session: Session):
    """Independent Implementation Review Remediation Plan v1/v1.2 §... small
    hardening — `status` is now `ProductionRunStatus | None`, so FastAPI/Pydantic
    rejects an invalid value with a structured 422, never an uncaught `ValueError`
    surfacing as a generic 500."""
    signup(client)
    response = client.get("/api/v1/production-runs?status=NOT_A_REAL_STATUS")
    assert response.status_code == 422, response.text


def test_start_production_cost_basis_unknown_via_http(client, session: Session):
    from app.db.models.ingredient import Ingredient
    from app.db.models.production import ProductionRequirement

    _business_obj, product, _order = _produced_setup(client, session)
    business = _business_obj
    ingredient = session.scalars(
        select(Ingredient).where(Ingredient.business_id == business.id)
    ).one()
    ingredient.physical_quantity = Decimal("0")
    ingredient.replacement_unit_cost = None
    ingredient.latest_purchase_unit_cost = None
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    response = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PRODUCTION_COST_BASIS_UNKNOWN"


def test_cancel_production_run_via_http(client, session: Session):
    from app.db.models.production import ProductionRequirement

    _business_obj, product, _order = _produced_setup(client, session)
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    start_response = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    run = start_response.json()

    cancel_response = client.post(
        f"/api/v1/production-runs/{run['id']}/cancel",
        json={"version": run["version"]},
        headers=csrf_headers(client),
    )
    assert cancel_response.status_code == 200, cancel_response.text
    assert cancel_response.json()["status"] == "CANCELED"


def test_manual_surplus_create_and_adjust_via_http(client, session: Session):
    signup(client)
    business = _business(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    session.commit()

    create_response = client.post(
        "/api/v1/surplus",
        json={
            "product_id": str(product.id),
            "quantity": "5",
            "unit_cost_basis": "1.50",
        },
        headers=csrf_headers(client),
    )
    assert create_response.status_code == 201, create_response.text
    lot = create_response.json()
    assert lot["source_production_run_id"] is None
    assert lot["source_total_cost_basis"] is None

    list_response = client.get("/api/v1/surplus")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1

    adjust_response = client.post(
        f"/api/v1/surplus/{lot['id']}/adjust",
        json={
            "version": lot["version"],
            "quantity_delta": "-2",
            "reason": "SPOILAGE_OR_WASTE",
        },
        headers=csrf_headers(client),
    )
    assert adjust_response.status_code == 200, adjust_response.text
    assert adjust_response.json()["physical_quantity"] == "3.000000"

    history_response = client.get(f"/api/v1/surplus/{lot['id']}/transactions")
    assert history_response.status_code == 200
    assert history_response.json()["total"] == 2  # creation + adjustment


def test_surplus_adjust_rejects_positive_delta_via_http(client, session: Session):
    signup(client)
    business = _business(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    session.commit()
    lot = client.post(
        "/api/v1/surplus",
        json={"product_id": str(product.id), "quantity": "5", "unit_cost_basis": "1.00"},
        headers=csrf_headers(client),
    ).json()

    response = client.post(
        f"/api/v1/surplus/{lot['id']}/adjust",
        json={"version": lot["version"], "quantity_delta": "5", "reason": "COUNT_CORRECTION"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
