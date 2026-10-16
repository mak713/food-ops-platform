"""Scenario builders shared by the Phase 9 Shopping List / Dashboard / cross-screen tests.

Everything is built through the real factories plus the real
`recalculate_product_closure` so the projection tables (requirements, reservations,
allocations) are exactly what production code would have produced — never hand-forged
derived state.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order
from app.db.models.product import Product
from app.services.operational_recalculation_service import recalculate_product_closure
from tests.integration.schema.factories import (
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)


def business_by_name(session: Session, name: str = "Test Bakery") -> Business:
    return session.scalars(select(Business).where(Business.name == name)).one()


def business_today(business: Business) -> date:
    return datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()


def make_recipe_product(
    session: Session,
    business: Business,
    *,
    name: str = "Cookie",
    yield_quantity: Decimal = Decimal("12"),
    ingredients: list[tuple[Ingredient, Decimal]] | None = None,
    elapsed_time_minutes: int | None = None,
    active_time_minutes: int = 30,
) -> Product:
    product = make_product(session, business, product_type=ProductType.PRODUCED, name=name)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session,
        business,
        recipe,
        yield_quantity=yield_quantity,
        active_time_minutes=active_time_minutes,
    )
    revision.elapsed_time_minutes = elapsed_time_minutes
    for ingredient, quantity in ingredients or []:
        make_recipe_revision_ingredient(
            session, business, revision, ingredient, quantity=quantity, unit="g"
        )
    session.flush()
    return product


def make_stocked_ingredient(
    session: Session, business: Business, *, name: str = "Flour", physical: str = "1000"
) -> Ingredient:
    ingredient = make_ingredient(session, business, name=name)
    ingredient.physical_quantity = Decimal(physical)
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    session.flush()
    return ingredient


def confirm_demand(
    session: Session,
    business: Business,
    product: Product,
    *,
    quantity: str,
    fulfillment_date: date,
    fulfillment_time: time | None = None,
    status: OrderStatus = OrderStatus.CONFIRMED,
    customer_id=None,
    order_number: str | None = None,
    recalc: bool = True,
    today: date | None = None,
    final_total: str = "25.00",
) -> Order:
    """A CONFIRMED (by default) Order with one CUSTOM_QUANTITY line for `product`, then the
    real operational recalculation so reservations/requirements exist."""
    kwargs = {"status": status, "customer_id": customer_id}
    if order_number is not None:
        kwargs["order_number"] = order_number
    order = make_order(session, business, **kwargs)
    order.fulfillment_date = fulfillment_date
    order.fulfillment_time = fulfillment_time
    order.final_total = Decimal(final_total)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal(quantity),
    )
    session.flush()
    if recalc:
        recalculate_product_closure(
            session, business, product, business_today=today or business_today(business)
        )
    session.commit()
    return order
