"""Whole-Order READY evaluation (Phase 8 Final Architecture Lock §10, corrected by
the Final Architecture Amendment — whole-Order, never produced-line-only).

READY is derived domain state: every V1-trackable line must be satisfied — produced
lines by completed `ProductionRunOrderAllocation` plus active `SurplusAllocation`
quantity covering `underlying_quantity`; purchased lines by the Product's aggregate
`purchased_shortage_quantity == 0` (Phase 7 tracks Purchased shortage only in
aggregate, with no per-Order allocation-priority mechanism, unlike Surplus's FEFO
priority — so no specific Order can be singled out as "the one that's covered" when
aggregate shortage exists); Custom one-off lines by `manual_fulfillment_satisfied`
when `manual_fulfillment_required` is set. Partial production never promotes a
mixed Order. `evaluate_and_apply_readiness` assumes the Order is already locked by
the caller (composed lock order: `Order` before `Product`) and never itself commits —
callers compose it into their own transaction, exactly like
`operational_recalculation_service.recalculate_product_closure`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.order import Order, OrderLine, OrderStatusHistory
from app.db.models.product import Product
from app.db.models.production import ProductionRunOrderAllocation
from app.db.models.purchased_inventory import PurchasedProductInventory
from app.db.models.surplus import PurchasedProductReservation, SurplusAllocation


def discover_candidate_order_ids(
    db: Session, business: Business, product_id: uuid.UUID
) -> list[uuid.UUID]:
    """Every CONFIRMED/READY Order currently referencing this Product — non-locking.
    Shared by every Phase 8 discover-lock-verify-retry protocol (Final Architecture
    Lock §11): `production_run_service.finish_production`'s recipient-Order
    discovery, Purchased restock/adjustment's readiness-promotion/demotion
    discovery, and Surplus manual-adjustment's discovery — one implementation, reused
    everywhere, so every caller's notion of "candidate Order set" is identical."""
    rows = db.scalars(
        select(Order.id)
        .join(OrderLine, OrderLine.order_id == Order.id)
        .where(
            OrderLine.product_id == product_id,
            OrderLine.business_id == business.id,
            Order.business_id == business.id,
            Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.READY)),
        )
        .distinct()
    ).all()
    return sorted(set(rows))


def purchased_shortage_for_product(db: Session, business: Business, product: Product) -> Decimal:
    """The Product-wide aggregate Purchased shortage (mirrors
    `operational_recalculation_service._recalculate_purchased_closure`'s own
    formula) — the only test Phase 7 supports for a purchased line's sufficiency,
    since there is no per-Order allocation priority for Purchased goods."""
    total_reserved: Decimal = db.scalar(
        select(func.coalesce(func.sum(PurchasedProductReservation.quantity), 0)).where(
            PurchasedProductReservation.product_id == product.id,
            PurchasedProductReservation.business_id == business.id,
        )
    )
    inventory_row = db.scalar(
        select(PurchasedProductInventory).where(
            PurchasedProductInventory.product_id == product.id,
            PurchasedProductInventory.business_id == business.id,
        )
    )
    physical_quantity = inventory_row.physical_quantity if inventory_row is not None else Decimal(0)
    return max(Decimal(0), total_reserved - physical_quantity)


def _line_is_satisfied(db: Session, business: Business, line: OrderLine) -> bool:
    if line.line_type == OrderLineType.CUSTOM_ITEM:
        if not line.manual_fulfillment_required:
            return True
        return line.manual_fulfillment_satisfied

    product = db.scalar(
        select(Product).where(Product.id == line.product_id, Product.business_id == business.id)
    )
    if product is None:
        return False

    if product.product_type == ProductType.PRODUCED:
        completed = db.scalar(
            select(func.coalesce(func.sum(ProductionRunOrderAllocation.quantity), 0)).where(
                ProductionRunOrderAllocation.order_line_id == line.id,
                ProductionRunOrderAllocation.business_id == business.id,
            )
        )
        active_surplus = db.scalar(
            select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
                SurplusAllocation.order_line_id == line.id,
                SurplusAllocation.business_id == business.id,
            )
        )
        return (completed + active_surplus) >= line.underlying_quantity

    return purchased_shortage_for_product(db, business, product) == 0


def is_order_fully_satisfied(db: Session, business: Business, order: Order) -> bool:
    if not order.lines:
        return False
    return all(_line_is_satisfied(db, business, line) for line in order.lines)


def evaluate_and_apply_readiness(db: Session, business: Business, order: Order) -> Order:
    """Promotes `CONFIRMED -> READY` when every line is now satisfied, or demotes
    `READY -> CONFIRMED` when a previously-satisfied Order no longer is (Spec §4.2:
    "domain logic may demote READY -> CONFIRMED, record the transition, and surface
    attention" — Completed Orders never demote, and this function is a no-op for
    every status other than CONFIRMED/READY). The caller must already hold this
    Order's row lock."""
    if order.status not in (OrderStatus.CONFIRMED, OrderStatus.READY):
        return order

    satisfied = is_order_fully_satisfied(db, business, order)

    if order.status is OrderStatus.CONFIRMED and satisfied:
        order.status = OrderStatus.READY
        order.ready_at = datetime.now(UTC)
        db.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                from_status=OrderStatus.CONFIRMED,
                to_status=OrderStatus.READY,
                changed_at=datetime.now(UTC),
            )
        )
    elif order.status is OrderStatus.READY and not satisfied:
        order.status = OrderStatus.CONFIRMED
        order.ready_at = None
        db.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                from_status=OrderStatus.READY,
                to_status=OrderStatus.CONFIRMED,
                changed_at=datetime.now(UTC),
            )
        )

    return order
