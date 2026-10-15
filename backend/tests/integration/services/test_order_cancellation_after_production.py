"""Integration tests for the corrected Order cancellation eligibility matrix
(Phase 8 Final Architecture Lock Addendum §A) — CONFIRMED and READY are both
cancelable; an IN_PRODUCTION-covered line hard-blocks; a completed-production-only
line requires explicit warning acknowledgment."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.order import Order
from app.db.models.production import ProductionRequirement, ProductionRunOrderAllocation
from app.db.models.purchased_inventory import PurchasedProductInventory
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.order_lifecycle_service import cancel_confirmed_order
from app.services.production_run_service import finish_production, start_production
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_purchased_inventory,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)

_TODAY = date(2026, 6, 1)


def test_confirmed_unproduced_cancel_succeeds(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("3"),
    )
    make_purchased_inventory(session, business, product, physical_quantity=10)
    session.commit()

    canceled, _results = cancel_confirmed_order(
        session, business, order.id, expected_version=order.version, business_today=_TODAY
    )
    session.commit()
    assert canceled.status == OrderStatus.CANCELED


def test_confirmed_with_active_run_blocked(session: Session):
    business = make_business_graph(session)
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
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    start_production(
        session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()

    with pytest.raises(ApiError) as exc_info:
        cancel_confirmed_order(
            session, business, order.id, expected_version=order.version, business_today=_TODAY
        )
    assert exc_info.value.code == "ORDER_PRODUCTION_LOCKED"


def test_confirmed_with_completed_production_requires_acknowledgment(session: Session):
    business = make_business_graph(session)
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
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    run = start_production(
        session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()
    finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("6"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(order)
    order_version = order.version

    with pytest.raises(ApiError) as exc_info:
        cancel_confirmed_order(
            session, business, order.id, expected_version=order_version, business_today=_TODAY
        )
    assert exc_info.value.code == "CANCELLATION_AFTER_PRODUCTION_REQUIRES_ACKNOWLEDGMENT"
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]

    # Untouched by the rejected attempt.
    allocation_before = session.scalars(
        select(ProductionRunOrderAllocation).where(
            ProductionRunOrderAllocation.production_run_id == run.id
        )
    ).one()

    canceled, _results = cancel_confirmed_order(
        session,
        business,
        order.id,
        expected_version=order_version,
        business_today=_TODAY,
        acknowledged_warning_fingerprint=fingerprint,
    )
    session.commit()

    assert canceled.status == OrderStatus.CANCELED
    # Historical rows are immutable -- never rewritten.
    allocation_after = session.scalars(
        select(ProductionRunOrderAllocation).where(
            ProductionRunOrderAllocation.id == allocation_before.id
        )
    ).one()
    assert allocation_after.quantity == allocation_before.quantity
    # No Ingredient restoration.
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("500.000000")


def test_ready_purchased_only_cancel_succeeds_and_releases_reservation(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("3"),
    )
    make_purchased_inventory(session, business, product, physical_quantity=10)
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    from app.services.readiness_service import evaluate_and_apply_readiness

    order = session.get(Order, order.id)
    evaluate_and_apply_readiness(session, business, order)
    session.commit()
    assert order.status == OrderStatus.READY

    canceled, _results = cancel_confirmed_order(
        session, business, order.id, expected_version=order.version, business_today=_TODAY
    )
    session.commit()

    assert canceled.status == OrderStatus.CANCELED
    assert canceled.ready_at is None
    reservation_count = session.scalar(
        select(PurchasedProductInventory).where(PurchasedProductInventory.product_id == product.id)
    )
    # Reservation released without physical deduction -- physical_quantity unchanged.
    assert reservation_count.physical_quantity == Decimal("10.000000")


def test_completed_order_cannot_be_canceled(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.COMPLETED)
    session.commit()

    with pytest.raises(ApiError) as exc_info:
        cancel_confirmed_order(
            session, business, order.id, expected_version=order.version, business_today=_TODAY
        )
    assert exc_info.value.code == "ORDER_NOT_CANCELABLE"


def test_canceled_order_cannot_be_canceled_again(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.CANCELED)
    session.commit()

    with pytest.raises(ApiError) as exc_info:
        cancel_confirmed_order(
            session, business, order.id, expected_version=order.version, business_today=_TODAY
        )
    assert exc_info.value.code == "ORDER_NOT_CANCELABLE"
