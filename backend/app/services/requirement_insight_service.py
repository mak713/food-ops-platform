"""Read-only Production Plan insight for each `ProductionRequirement` (Spec §12.11:
shortages and affected fulfillments; §20.13: the same shortage must appear consistently
in the Production Plan, Shopping List, Inventory, and Dashboard — Phase 9 approved
cross-screen consistency work).

For every Ingredient a requirement needs, the shortage shown is the ADR-130 cumulative
shortage *as of that requirement's demand date* — computed by the identical pure
calculator (`app.domain.shopping_list.shortage_through`) over the identical reservation
stream (`availability_service.reservation_streams`) the Shopping List uses, so the two
screens cannot disagree. It is explicitly an "as of this date" figure (stock is shared
across earlier demand), not a share attributable to this requirement alone.

Takes no locks and writes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order
from app.db.models.production import (
    ProductionIngredientRequirement,
    ProductionRequirement,
    ProductionRequirementOrder,
)
from app.domain import shopping_list as shopping_domain
from app.services import availability_service


@dataclass(frozen=True)
class RequirementIngredientNeed:
    ingredient_id: uuid.UUID
    ingredient_name: str
    canonical_unit: str
    required_quantity: Decimal
    physical_quantity: Decimal
    shortage_quantity: Decimal
    """Cumulative shortage of this Ingredient through the requirement's demand date."""


@dataclass(frozen=True)
class RequirementAffectedOrder:
    order_id: uuid.UUID
    order_number: str
    status: str
    customer_name: str | None
    fulfillment_date: date | None
    fulfillment_time: time | None
    demand_quantity: Decimal
    """The Order's own product-unit demand feeding this requirement (Spec §8.20)."""


@dataclass(frozen=True)
class RequirementInsight:
    ingredient_needs: list[RequirementIngredientNeed] = field(default_factory=list)
    affected_orders: list[RequirementAffectedOrder] = field(default_factory=list)

    @property
    def has_shortage(self) -> bool:
        return any(need.shortage_quantity > 0 for need in self.ingredient_needs)


def build_requirement_insights(
    db: Session, business: Business, requirements: Sequence[ProductionRequirement]
) -> dict[uuid.UUID, RequirementInsight]:
    if not requirements:
        return {}
    requirement_ids = [r.id for r in requirements]
    demand_dates = {r.id: r.demand_date for r in requirements}

    need_rows = db.execute(
        select(
            ProductionIngredientRequirement.production_requirement_id,
            ProductionIngredientRequirement.required_quantity_canonical,
            Ingredient,
        )
        .join(Ingredient, Ingredient.id == ProductionIngredientRequirement.ingredient_id)
        .where(
            ProductionIngredientRequirement.business_id == business.id,
            Ingredient.business_id == business.id,
            ProductionIngredientRequirement.production_requirement_id.in_(requirement_ids),
        )
    ).all()
    ingredient_ids = {ingredient.id for _req, _qty, ingredient in need_rows}
    streams = availability_service.reservation_streams(db, business, ingredient_ids)

    needs: dict[uuid.UUID, list[RequirementIngredientNeed]] = {}
    for requirement_id, required_quantity, ingredient in need_rows:
        shortage = shopping_domain.shortage_through(
            streams.get(ingredient.id, []),
            ingredient.physical_quantity,
            demand_dates[requirement_id],
        )
        needs.setdefault(requirement_id, []).append(
            RequirementIngredientNeed(
                ingredient_id=ingredient.id,
                ingredient_name=ingredient.name,
                canonical_unit=ingredient.canonical_unit,
                required_quantity=required_quantity,
                physical_quantity=ingredient.physical_quantity,
                shortage_quantity=shortage,
            )
        )
    for entries in needs.values():
        entries.sort(key=lambda n: (-n.shortage_quantity, n.ingredient_name.casefold()))

    link_rows = db.execute(
        select(
            ProductionRequirementOrder.production_requirement_id,
            Order.id,
            Order.order_number,
            Order.status,
            Order.fulfillment_date,
            Order.fulfillment_time,
            Customer.name,
            ProductionRequirementOrder.demand_quantity,
        )
        .join(Order, Order.id == ProductionRequirementOrder.order_id)
        .outerjoin(
            Customer, (Customer.id == Order.customer_id) & (Customer.business_id == business.id)
        )
        .where(
            ProductionRequirementOrder.business_id == business.id,
            Order.business_id == business.id,
            ProductionRequirementOrder.production_requirement_id.in_(requirement_ids),
        )
    ).all()
    merged: dict[tuple[uuid.UUID, uuid.UUID], RequirementAffectedOrder] = {}
    for requirement_id, order_id, number, status, f_date, f_time, customer, qty in link_rows:
        key = (requirement_id, order_id)
        existing = merged.get(key)
        merged[key] = RequirementAffectedOrder(
            order_id=order_id,
            order_number=number,
            status=status.value,
            customer_name=customer,
            fulfillment_date=f_date,
            fulfillment_time=f_time,
            demand_quantity=qty if existing is None else existing.demand_quantity + qty,
        )
    orders: dict[uuid.UUID, list[RequirementAffectedOrder]] = {}
    for (requirement_id, _order_id), affected in merged.items():
        orders.setdefault(requirement_id, []).append(affected)
    for entries in orders.values():
        entries.sort(
            key=lambda o: (
                o.fulfillment_date or date.max,
                o.fulfillment_time or time.max,
                o.order_number,
            )
        )

    return {
        requirement_id: RequirementInsight(
            ingredient_needs=needs.get(requirement_id, []),
            affected_orders=orders.get(requirement_id, []),
        )
        for requirement_id in requirement_ids
    }
