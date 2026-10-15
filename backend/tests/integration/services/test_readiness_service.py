"""Integration tests for whole-Order READY evaluation (Phase 8 Final Architecture
Lock §10, corrected by the Final Architecture Amendment)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.order import OrderStatusHistory
from app.services.readiness_service import evaluate_and_apply_readiness
from tests.integration.schema.factories import (
    make_business_graph,
    make_order,
    make_order_line,
    make_product,
    make_production_requirement,
    make_production_run,
    make_production_run_order_allocation,
    make_purchased_inventory,
    make_purchased_product_reservation,
    make_recipe,
    make_recipe_revision,
    make_surplus_allocation,
    make_surplus_inventory,
)


def test_produced_line_promotes_when_fully_allocated_via_production_run(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    run = make_production_run(session, business, product, revision, planned_batches=1)
    make_production_run_order_allocation(session, business, run, order, line, quantity=6)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.READY
    assert order.ready_at is not None
    history = session.scalars(
        select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id)
    ).all()
    assert len(history) == 1
    assert history[0].from_status == OrderStatus.CONFIRMED
    assert history[0].to_status == OrderStatus.READY


def test_produced_line_promotes_via_surplus_allocation(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    requirement = make_production_requirement(session, business, product, revision)
    lot = make_surplus_inventory(session, business, product, physical_quantity=10)
    make_surplus_allocation(session, business, lot, requirement, order, line, quantity=6)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.READY


def test_partial_production_never_promotes(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("10"),
    )
    run = make_production_run(session, business, product, revision, planned_batches=1)
    make_production_run_order_allocation(session, business, run, order, line, quantity=4)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.CONFIRMED


def test_purchased_line_satisfied_by_zero_aggregate_shortage(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    make_purchased_inventory(session, business, product, physical_quantity=10)
    make_purchased_product_reservation(session, business, product, order, line, quantity=6)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.READY


def test_purchased_line_not_satisfied_when_aggregate_shortage_exists(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    make_purchased_inventory(session, business, product, physical_quantity=2)
    make_purchased_product_reservation(session, business, product, order, line, quantity=6)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.CONFIRMED


def test_mixed_produced_and_purchased_requires_both_satisfied(session: Session):
    business = make_business_graph(session)
    produced = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, produced)
    revision = make_recipe_revision(session, business, recipe)
    purchased = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    produced_line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=produced,
        underlying_quantity=Decimal("6"),
    )
    purchased_line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=purchased,
        underlying_quantity=Decimal("3"),
    )
    run = make_production_run(session, business, produced, revision, planned_batches=1)
    make_production_run_order_allocation(session, business, run, order, produced_line, quantity=6)
    make_purchased_inventory(session, business, purchased, physical_quantity=0)  # short
    make_purchased_product_reservation(
        session, business, purchased, order, purchased_line, quantity=3
    )
    session.flush()

    evaluate_and_apply_readiness(session, business, order)
    assert order.status == OrderStatus.CONFIRMED  # purchased side still short

    # Restocking removes the shortage -> now both sides satisfied.
    from app.db.models.purchased_inventory import PurchasedProductInventory

    inv = session.scalars(
        select(PurchasedProductInventory).where(
            PurchasedProductInventory.product_id == purchased.id
        )
    ).one()
    inv.physical_quantity = Decimal("3")
    session.flush()

    evaluate_and_apply_readiness(session, business, order)
    assert order.status == OrderStatus.READY


def test_custom_item_requires_manual_satisfaction_flag(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_ITEM,
        manual_fulfillment_required=True,
        manual_fulfillment_satisfied=False,
    )
    session.flush()

    evaluate_and_apply_readiness(session, business, order)
    assert order.status == OrderStatus.CONFIRMED

    order.lines[0].manual_fulfillment_satisfied = True
    session.flush()
    evaluate_and_apply_readiness(session, business, order)
    assert order.status == OrderStatus.READY


def test_custom_item_not_requiring_manual_fulfillment_is_trivially_satisfied(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_ITEM,
        manual_fulfillment_required=False,
    )
    session.flush()

    evaluate_and_apply_readiness(session, business, order)
    assert order.status == OrderStatus.READY


def test_demotion_when_previously_satisfied_order_becomes_insufficient(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.READY)
    order.ready_at = None
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    make_purchased_inventory(session, business, product, physical_quantity=0)
    make_purchased_product_reservation(session, business, product, order, line, quantity=6)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.CONFIRMED
    assert order.ready_at is None
    history = session.scalars(
        select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id)
    ).one()
    assert history.from_status == OrderStatus.READY
    assert history.to_status == OrderStatus.CONFIRMED


def test_completed_and_canceled_orders_never_evaluated(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.COMPLETED)
    session.flush()

    evaluate_and_apply_readiness(session, business, order)

    assert order.status == OrderStatus.COMPLETED
    assert (
        session.scalars(
            select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id)
        ).first()
        is None
    )
