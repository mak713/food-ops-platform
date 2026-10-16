"""Scenario builders for the Order Completion tests. READY Orders are produced through the
real recalculation + the real readiness evaluation (never forged), so the projection rows
Completion consumes (reservations, Surplus allocations) are exactly what production code
creates."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.order import Order
from app.db.models.product import Product
from app.services import readiness_service
from app.services.operational_recalculation_service import recalculate_product_closure
from tests.integration.api.phase9_support import business_today, make_recipe_product
from tests.integration.schema.factories import (
    make_order,
    make_order_line,
    make_product,
    make_purchased_inventory,
    make_surplus_inventory,
)


def purchased_product(
    session: Session,
    business: Business,
    *,
    name: str = "Lemonade",
    physical: str | None = "10",
    wac: str = "1.50",
) -> Product:
    product = make_product(session, business, product_type=ProductType.PURCHASED, name=name)
    session.flush()
    if physical is not None:
        inventory = make_purchased_inventory(
            session, business, product, physical_quantity=Decimal(physical)
        )
        inventory.weighted_average_unit_cost = Decimal(wac)
        inventory.latest_purchase_unit_cost = Decimal("9.99")  # must never participate
        inventory.replacement_unit_cost = Decimal("8.88")  # must never participate
    session.flush()
    return product


def produced_product(
    session: Session, business: Business, *, name: str = "Cookie", ingredients=None
) -> Product:
    return make_recipe_product(
        session, business, name=name, yield_quantity=Decimal("12"), ingredients=ingredients
    )


def surplus_lot(
    session: Session, business: Business, product: Product, *, physical: str, basis: str
):
    lot = make_surplus_inventory(
        session,
        business,
        product,
        physical_quantity=Decimal(physical),
        unit_cost_basis=Decimal(basis),
    )
    session.flush()
    return lot


def build_ready_order(
    session: Session,
    business: Business,
    lines: list[dict],
    *,
    fulfillment_offset_days: int = 1,
    order_number: str | None = None,
    expect_status: OrderStatus | None = OrderStatus.READY,
    fulfillment_time=None,
) -> Order:
    """Creates a CONFIRMED Order with the described lines, runs the real recalculation for
    every referenced Product, then the real readiness evaluation, and commits.

    Each line dict: `product` (Product | None), `qty`, optional `packaging` (str total
    snapshot), `custom_estimate` (str | None, CUSTOM_ITEM only), `manual_required` (bool)."""
    today = business_today(business)
    kwargs = {"status": OrderStatus.CONFIRMED}
    if order_number is not None:
        kwargs["order_number"] = order_number
    order = make_order(session, business, **kwargs)
    order.fulfillment_date = today + timedelta(days=fulfillment_offset_days)
    order.fulfillment_time = fulfillment_time
    order.final_total = Decimal("40.00")
    products: dict = {}
    for spec in lines:
        product = spec.get("product")
        line_type = OrderLineType.CUSTOM_ITEM if product is None else OrderLineType.CUSTOM_QUANTITY
        line = make_order_line(
            session,
            business,
            order,
            line_type=line_type,
            product=product,
            underlying_quantity=Decimal(spec["qty"]),
            manual_fulfillment_required=spec.get("manual_required", False),
        )
        line.packaging_cost_total_snapshot = Decimal(spec.get("packaging", "0.00"))
        if "custom_estimate" in spec:
            line.custom_direct_cost_estimate = (
                None if spec["custom_estimate"] is None else Decimal(spec["custom_estimate"])
            )
        if product is not None:
            products[product.id] = product
    session.flush()
    for product in products.values():
        recalculate_product_closure(session, business, product, business_today=today)
    session.flush()
    readiness_service.evaluate_and_apply_readiness(session, business, order)
    session.commit()
    if expect_status is not None:
        assert order.status is expect_status, f"expected {expect_status}, got {order.status}"
    return order


def domain_fingerprint(session: Session) -> dict[str, tuple[int, str]]:
    """Whole-row md5 fingerprint of every domain table (zero-write assertions)."""
    skip = {"sessions", "password_reset_tokens", "users"}
    fingerprint = {}
    for table in Base.metadata.sorted_tables:
        if table.name in skip:
            continue
        row = session.execute(
            text(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, ',' ORDER BY t::text)), '') "
                f"FROM {table.name} t"
            )
        ).one()
        fingerprint[table.name] = (row[0], row[1])
    return fingerprint


def reload(session: Session, model, id_):
    session.expire_all()
    return session.scalars(select(model).where(model.id == id_)).one()


def as_of(day_offset: int = 0) -> date:
    return date.today() + timedelta(days=day_offset)
