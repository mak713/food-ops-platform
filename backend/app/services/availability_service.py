"""Read-only derivations of reserved/allocated quantities (Spec §7.3, INV-004/005, SUR-003;
Phase 9 approved cross-screen consistency work).

One authoritative reader of the reservation/allocation projection tables, so the
Ingredient screens, the Surplus screens, the Production Plan, the Shopping List, and the
Dashboard all see identical numbers rather than each re-deriving them. This module only
SELECTs — it takes no locks and writes nothing, so it can never participate in any
workflow lock order (ADR-124/141).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.business import Business
from app.db.models.production import IngredientReservation, ProductionRequirement
from app.db.models.surplus import SurplusAllocation


def ingredient_reserved_quantities(
    db: Session, business: Business, ingredient_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """`SUM(active IngredientReservation.quantity_canonical)` per Ingredient (tenant
    scoped). Ingredients with no reservation map to `Decimal(0)`."""
    ids = set(ingredient_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(
            IngredientReservation.ingredient_id,
            func.coalesce(func.sum(IngredientReservation.quantity_canonical), 0),
        )
        .where(
            IngredientReservation.business_id == business.id,
            IngredientReservation.ingredient_id.in_(ids),
        )
        .group_by(IngredientReservation.ingredient_id)
    ).all()
    totals = {ingredient_id: Decimal(total) for ingredient_id, total in rows}
    return {ingredient_id: totals.get(ingredient_id, Decimal(0)) for ingredient_id in ids}


def surplus_allocated_quantities(
    db: Session, business: Business, lot_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """`SUM(active SurplusAllocation.quantity)` per Surplus lot (tenant scoped). Lots
    with no allocation map to `Decimal(0)`."""
    ids = set(lot_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(
            SurplusAllocation.surplus_inventory_id,
            func.coalesce(func.sum(SurplusAllocation.quantity), 0),
        )
        .where(
            SurplusAllocation.business_id == business.id,
            SurplusAllocation.surplus_inventory_id.in_(ids),
        )
        .group_by(SurplusAllocation.surplus_inventory_id)
    ).all()
    totals = {lot_id: Decimal(total) for lot_id, total in rows}
    return {lot_id: totals.get(lot_id, Decimal(0)) for lot_id in ids}


def reservation_streams(
    db: Session,
    business: Business,
    ingredient_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """The chronological reservation stream per Ingredient —
    `[(requirement.demand_date, reservation.quantity_canonical), ...]` — the input to the
    ADR-130 cumulative-shortage calculators. `None` means every Ingredient with an
    active reservation."""
    stmt = (
        select(
            IngredientReservation.ingredient_id,
            ProductionRequirement.demand_date,
            IngredientReservation.quantity_canonical,
        )
        .join(
            ProductionRequirement,
            IngredientReservation.production_requirement_id == ProductionRequirement.id,
        )
        .where(
            IngredientReservation.business_id == business.id,
            ProductionRequirement.business_id == business.id,
        )
        .order_by(ProductionRequirement.demand_date.asc())
    )
    if ingredient_ids is not None:
        ids = set(ingredient_ids)
        if not ids:
            return {}
        stmt = stmt.where(IngredientReservation.ingredient_id.in_(ids))
    streams: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
    for ingredient_id, demand_date, quantity in db.execute(stmt).all():
        streams.setdefault(ingredient_id, []).append((demand_date, quantity))
    return streams
