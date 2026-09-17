"""Shopping List backend derivation (Spec §6.10 PLAN-011; Phase 7 Plan v2 §14,
corrected by the Final Pre-Implementation Amendment §1).

Read-only, Ingredient-only (Purchased-Product shortages remain operational
warnings/reservation state, never a Shopping List line — no authoritative spec
requirement supports listing them here). No persisted state — every result is
derived at read time from `Ingredient.physical_quantity` and the authoritative,
chronological `ingredient_reservations` stream.
"""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.production import IngredientReservation, ProductionRequirement

_CONTEXT_PRECISION = 50
"""Matches the isolation precision every Phase 7 deterministic calculator uses
(ADR-109) — this module's cumulative Decimal arithmetic must not depend on the
caller's ambient global Decimal context."""


@dataclass(frozen=True)
class ShoppingListDateShortage:
    demand_date: date
    cumulative_reserved_through_date: Decimal
    cumulative_shortage_through_date: Decimal


@dataclass(frozen=True)
class ShoppingListIngredientResult:
    ingredient_id: uuid.UUID
    ingredient_name: str
    physical_quantity: Decimal
    by_date: list[ShoppingListDateShortage]


def derive_shopping_list(
    db: Session,
    business: Business,
    *,
    business_today: date,
    horizon_days: int,
) -> list[ShoppingListIngredientResult]:
    """For each Ingredient with any active reservation, walks demand dates within
    `[business_today, business_today + horizon_days]` and computes cumulative
    reserved/shortage through each date (Amendment §1's corrected formula):

    - `cumulative_reserved_through_D = SUM(reservation.quantity_canonical WHERE
      demand_date <= D)` — this naturally includes every overdue active
      reservation for every `D` in the horizon (an overdue reservation's
      `demand_date` is always `<= D`), and never includes a reservation dated
      after `D`, so a reservation beyond the requested horizon can never inflate
      an in-horizon result.
    - `cumulative_shortage_through_D = max(0, cumulative_reserved_through_D -
      Ingredient.physical_quantity)`.

    No separate "available" pool is computed — `physical_quantity` is the single
    subtracted term at each date threshold, never subtracted a second time.
    """
    rows = db.execute(
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
    ).all()

    by_ingredient: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
    for ingredient_id, demand_date, quantity in rows:
        by_ingredient.setdefault(ingredient_id, []).append((demand_date, quantity))

    if not by_ingredient:
        return []

    ingredients = {
        ing.id: ing
        for ing in db.scalars(
            select(Ingredient).where(
                Ingredient.id.in_(by_ingredient.keys()), Ingredient.business_id == business.id
            )
        )
    }

    display_dates = [
        date.fromordinal(business_today.toordinal() + offset) for offset in range(horizon_days + 1)
    ]

    results: list[ShoppingListIngredientResult] = []
    for ingredient_id, reservations in by_ingredient.items():
        ingredient = ingredients.get(ingredient_id)
        if ingredient is None:
            continue
        reservations_sorted = sorted(reservations, key=lambda r: r[0])
        by_date: list[ShoppingListDateShortage] = []
        for display_date in display_dates:
            with decimal.localcontext() as ctx:
                ctx.prec = _CONTEXT_PRECISION
                cumulative_reserved = sum(
                    (
                        qty
                        for demand_date, qty in reservations_sorted
                        if demand_date <= display_date
                    ),
                    Decimal(0),
                )
                cumulative_shortage = max(
                    Decimal(0), cumulative_reserved - ingredient.physical_quantity
                )
            by_date.append(
                ShoppingListDateShortage(
                    demand_date=display_date,
                    cumulative_reserved_through_date=cumulative_reserved,
                    cumulative_shortage_through_date=cumulative_shortage,
                )
            )
        results.append(
            ShoppingListIngredientResult(
                ingredient_id=ingredient_id,
                ingredient_name=ingredient.name,
                physical_quantity=ingredient.physical_quantity,
                by_date=by_date,
            )
        )

    return results
