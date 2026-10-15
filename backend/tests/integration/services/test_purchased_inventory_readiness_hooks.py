"""Integration tests for the Purchased-inventory readiness hooks (Phase 8 Final
Architecture Lock §11/Final Architecture Amendment §B) — a restock may promote a
waiting Order to READY; a downward Manual Adjustment may demote a READY Order back
to CONFIRMED."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.schemas.inventory import (
    ManualAdjustmentReason,
    PurchasedAdjustmentRequest,
    PurchasedRestockRequest,
)
from app.services.purchased_inventory_service import (
    adjust_purchased_product,
    restock_purchased_product,
)
from tests.integration.schema.factories import (
    make_business_graph,
    make_order,
    make_order_line,
    make_product,
    make_purchased_inventory,
    make_purchased_product_reservation,
)


def test_restock_promotes_waiting_order_to_ready(session: Session):
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
    inventory = make_purchased_inventory(session, business, product, physical_quantity=0)
    make_purchased_product_reservation(session, business, product, order, line, quantity=6)
    session.commit()

    restock_purchased_product(
        session,
        business,
        product.id,
        PurchasedRestockRequest(
            quantity=Decimal("10"), unit_cost=Decimal("1.00"), version=inventory.version
        ),
    )
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.READY


def test_downward_adjustment_demotes_ready_order(session: Session):
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
    inventory = make_purchased_inventory(session, business, product, physical_quantity=6)
    make_purchased_product_reservation(session, business, product, order, line, quantity=6)
    session.commit()

    adjust_purchased_product(
        session,
        business,
        product.id,
        PurchasedAdjustmentRequest(
            quantity_change=Decimal("-4"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            version=inventory.version,
        ),
    )
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED


def test_restock_with_no_outstanding_demand_takes_cheap_path(session: Session):
    """No CONFIRMED/READY Order references this Product -- no Order/Product lock
    escalation needed, behaves exactly as the pre-Phase-8 single-row mutation."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    inventory = make_purchased_inventory(session, business, product, physical_quantity=0)
    session.commit()

    updated = restock_purchased_product(
        session,
        business,
        product.id,
        PurchasedRestockRequest(
            quantity=Decimal("10"), unit_cost=Decimal("1.00"), version=inventory.version
        ),
    )
    session.commit()

    assert updated.physical_quantity == Decimal("10.000000")
