"""Shopping List derivation (Spec §6.10 PLAN-011, §12.12; ADR-130; Phase 9 report).

Read-only, Ingredient-only (Purchased-Product shortages remain operational
warnings/reservation state, never a Shopping List line — no authoritative spec
requirement supports listing them here). No persisted state — every result is
derived at read time from `Ingredient.physical_quantity` and the authoritative,
chronological `ingredient_reservations` stream. Takes no locks and writes nothing.

Two entry points share one derivation:

* `derive_shopping_list` — the Phase 7 backend foundation (unchanged contract), used as
  the regression net for ADR-130.
* `build_shopping_list_report` — the Phase 9 report: a validated range (preset or custom),
  shortage-first ordering, and a contributing-demand drilldown (requirements and the
  Orders that feed them). Ingredient quantities are *not* apportioned to individual
  Orders — the spec defines no such rule and §17.15 forbids guessing — so the drilldown
  shows requirement-level reserved quantity plus each contributing Order's own
  product-unit demand.

The arithmetic itself lives in `app.domain.shopping_list` (pure, ADR-109); the dashboard's
ingredient-shortage alert and the Production Plan's per-requirement shortage both call
into the same calculators, which is what keeps every screen numerically identical
(Spec §20.13).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import CalculationStatus, ProductionRunStatus
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order
from app.db.models.product import Product
from app.db.models.production import (
    IngredientReservation,
    ProductionRequirement,
    ProductionRequirementOrder,
    ProductionRun,
)
from app.domain import shopping_list as shopping_domain
from app.domain.shopping_list import DateShortage, ResolvedShoppingRange
from app.services import availability_service

ShoppingListDateShortage = DateShortage
"""Kept as the module's public name for a timeline entry (Phase 7 contract)."""


@dataclass(frozen=True)
class ShoppingListIngredientResult:
    ingredient_id: uuid.UUID
    ingredient_name: str
    physical_quantity: Decimal
    by_date: list[DateShortage]


def derive_shopping_list(
    db: Session,
    business: Business,
    *,
    business_today: date,
    horizon_days: int,
) -> list[ShoppingListIngredientResult]:
    """For each Ingredient with any active reservation, walks demand dates within
    `[business_today, business_today + horizon_days]` and computes cumulative
    reserved/shortage through each date (ADR-130's corrected formula):

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
    streams = availability_service.reservation_streams(db, business)
    if not streams:
        return []

    ingredients = {
        ing.id: ing
        for ing in db.scalars(
            select(Ingredient).where(
                Ingredient.id.in_(streams.keys()), Ingredient.business_id == business.id
            )
        )
    }

    display_dates = [
        date.fromordinal(business_today.toordinal() + offset) for offset in range(horizon_days + 1)
    ]

    results: list[ShoppingListIngredientResult] = []
    for ingredient_id, reservations in streams.items():
        ingredient = ingredients.get(ingredient_id)
        if ingredient is None:
            continue
        results.append(
            ShoppingListIngredientResult(
                ingredient_id=ingredient_id,
                ingredient_name=ingredient.name,
                physical_quantity=ingredient.physical_quantity,
                by_date=shopping_domain.build_timeline(
                    reservations, ingredient.physical_quantity, display_dates
                ),
            )
        )
    return results


# ------------------------------------------------------------------------------------
# Phase 9 report
# ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DrilldownOrder:
    order_id: uuid.UUID
    order_number: str
    status: str
    customer_name: str | None
    fulfillment_date: date | None
    fulfillment_time: time | None
    demand_quantity: Decimal
    """The Order's own demand for the requirement's Product, in underlying product units
    (Spec §8.20) — never an Ingredient quantity."""


@dataclass(frozen=True)
class DrilldownContributor:
    requirement_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    recipe_revision_id: uuid.UUID | None
    demand_date: date
    reserved_quantity: Decimal
    is_overdue: bool
    is_in_production: bool
    """True when an `IN_PRODUCTION` run already owns this requirement (ADR-126) — the
    reservation is real and still counts toward the shortage until Finish consumes it."""
    orders: list[DrilldownOrder] = field(default_factory=list)


@dataclass(frozen=True)
class ShoppingListItem:
    ingredient_id: uuid.UUID
    ingredient_name: str
    canonical_unit: str
    physical_quantity: Decimal
    reserved_through_end: Decimal
    shortage_quantity: Decimal
    """`cumulative_shortage_through(range.end_date)` — what must be bought to cover every
    reservation dated up to the end of the selected range."""
    needed_by_date: date | None
    by_date: list[DateShortage]
    contributors: list[DrilldownContributor]


@dataclass(frozen=True)
class IncompleteRecipeNotice:
    requirement_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    demand_date: date
    demand_quantity: Decimal


@dataclass(frozen=True)
class ShoppingListReport:
    range: ResolvedShoppingRange
    shortages: list[ShoppingListItem]
    """Ingredients with a shortage at the end of the range — shortage-first ordering."""
    covered: list[ShoppingListItem]
    """Ingredients reserved within the range that current stock fully covers."""
    incomplete_recipe: list[IncompleteRecipeNotice]
    """Confirmed demand whose Ingredient needs cannot be calculated (no applicable
    Recipe) — surfaced, never guessed (Spec §14.10, §17.15)."""


def _load_contributors(
    db: Session,
    business: Business,
    *,
    ingredient_ids: set[uuid.UUID],
    end_date: date,
    business_today: date,
) -> dict[uuid.UUID, list[DrilldownContributor]]:
    if not ingredient_ids:
        return {}

    reservation_rows = db.execute(
        select(
            IngredientReservation.ingredient_id,
            IngredientReservation.quantity_canonical,
            ProductionRequirement.id,
            ProductionRequirement.product_id,
            ProductionRequirement.recipe_revision_id,
            ProductionRequirement.demand_date,
            Product.name,
        )
        .join(
            ProductionRequirement,
            IngredientReservation.production_requirement_id == ProductionRequirement.id,
        )
        .join(Product, Product.id == ProductionRequirement.product_id)
        .where(
            IngredientReservation.business_id == business.id,
            ProductionRequirement.business_id == business.id,
            Product.business_id == business.id,
            IngredientReservation.ingredient_id.in_(ingredient_ids),
            ProductionRequirement.demand_date <= end_date,
        )
    ).all()
    requirement_ids = {row[2] for row in reservation_rows}

    protected_ids: set[uuid.UUID] = set()
    orders_by_requirement: dict[uuid.UUID, list[DrilldownOrder]] = {}
    if requirement_ids:
        protected_ids = set(
            db.scalars(
                select(ProductionRun.source_production_requirement_id).where(
                    ProductionRun.business_id == business.id,
                    ProductionRun.status == ProductionRunStatus.IN_PRODUCTION,
                    ProductionRun.source_production_requirement_id.in_(requirement_ids),
                )
            )
        )
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
                Customer,
                (Customer.id == Order.customer_id) & (Customer.business_id == business.id),
            )
            .where(
                ProductionRequirementOrder.business_id == business.id,
                Order.business_id == business.id,
                ProductionRequirementOrder.production_requirement_id.in_(requirement_ids),
            )
        ).all()
        merged: dict[tuple[uuid.UUID, uuid.UUID], DrilldownOrder] = {}
        for req_id, order_id, number, status, f_date, f_time, customer, qty in link_rows:
            existing = merged.get((req_id, order_id))
            if existing is None:
                merged[(req_id, order_id)] = DrilldownOrder(
                    order_id=order_id,
                    order_number=number,
                    status=status.value,
                    customer_name=customer,
                    fulfillment_date=f_date,
                    fulfillment_time=f_time,
                    demand_quantity=qty,
                )
            else:
                # An Order may feed one requirement through several lines — one row per
                # Order, with its product-unit demand summed.
                merged[(req_id, order_id)] = DrilldownOrder(
                    order_id=existing.order_id,
                    order_number=existing.order_number,
                    status=existing.status,
                    customer_name=existing.customer_name,
                    fulfillment_date=existing.fulfillment_date,
                    fulfillment_time=existing.fulfillment_time,
                    demand_quantity=existing.demand_quantity + qty,
                )
        for (req_id, _order_id), drill in merged.items():
            orders_by_requirement.setdefault(req_id, []).append(drill)
        for drills in orders_by_requirement.values():
            drills.sort(
                key=lambda d: (
                    d.fulfillment_date or date.max,
                    d.fulfillment_time or time.max,
                    d.order_number,
                )
            )

    by_ingredient: dict[uuid.UUID, list[DrilldownContributor]] = {}
    for (
        ingredient_id,
        qty,
        req_id,
        product_id,
        revision_id,
        demand_date,
        product_name,
    ) in reservation_rows:
        by_ingredient.setdefault(ingredient_id, []).append(
            DrilldownContributor(
                requirement_id=req_id,
                product_id=product_id,
                product_name=product_name,
                recipe_revision_id=revision_id,
                demand_date=demand_date,
                reserved_quantity=qty,
                is_overdue=demand_date < business_today,
                is_in_production=req_id in protected_ids,
                orders=orders_by_requirement.get(req_id, []),
            )
        )
    for contributors in by_ingredient.values():
        contributors.sort(
            key=lambda c: (c.demand_date, c.product_name.casefold(), c.requirement_id)
        )
    return by_ingredient


def build_shopping_list_report(
    db: Session,
    business: Business,
    *,
    business_today: date,
    shopping_range: ResolvedShoppingRange,
) -> ShoppingListReport:
    """Builds the Phase 9 Shopping List for an already-validated range. Every figure is
    derived at read time; nothing is persisted or locked."""
    streams = availability_service.reservation_streams(db, business)
    ingredients = (
        {
            ing.id: ing
            for ing in db.scalars(
                select(Ingredient).where(
                    Ingredient.id.in_(streams.keys()), Ingredient.business_id == business.id
                )
            )
        }
        if streams
        else {}
    )

    display_dates = shopping_range.display_dates()
    timelines: dict[uuid.UUID, list[DateShortage]] = {}
    for ingredient_id, reservations in streams.items():
        ingredient = ingredients.get(ingredient_id)
        if ingredient is None:
            continue
        timeline = shopping_domain.build_timeline(
            reservations, ingredient.physical_quantity, display_dates
        )
        # An Ingredient with nothing reserved up to the end of the range is irrelevant to
        # this range (its reservations all lie beyond it).
        if timeline[-1].cumulative_reserved_through_date > 0:
            timelines[ingredient_id] = timeline

    contributors = _load_contributors(
        db,
        business,
        ingredient_ids=set(timelines),
        end_date=shopping_range.end_date,
        business_today=business_today,
    )

    shortages: list[ShoppingListItem] = []
    covered: list[ShoppingListItem] = []
    for ingredient_id, timeline in timelines.items():
        ingredient = ingredients[ingredient_id]
        item = ShoppingListItem(
            ingredient_id=ingredient_id,
            ingredient_name=ingredient.name,
            canonical_unit=ingredient.canonical_unit,
            physical_quantity=ingredient.physical_quantity,
            reserved_through_end=timeline[-1].cumulative_reserved_through_date,
            shortage_quantity=timeline[-1].cumulative_shortage_through_date,
            needed_by_date=shopping_domain.first_shortage_date(timeline),
            by_date=timeline,
            contributors=contributors.get(ingredient_id, []),
        )
        (shortages if item.shortage_quantity > 0 else covered).append(item)

    shortages.sort(
        key=lambda i: (-i.shortage_quantity, i.ingredient_name.casefold(), i.ingredient_id)
    )
    covered.sort(key=lambda i: (i.ingredient_name.casefold(), i.ingredient_id))

    incomplete_rows = db.execute(
        select(
            ProductionRequirement.id,
            ProductionRequirement.product_id,
            Product.name,
            ProductionRequirement.demand_date,
            ProductionRequirement.production_demand_quantity,
        )
        .join(Product, Product.id == ProductionRequirement.product_id)
        .where(
            ProductionRequirement.business_id == business.id,
            Product.business_id == business.id,
            ProductionRequirement.calculation_status == CalculationStatus.INCOMPLETE_RECIPE,
            ProductionRequirement.demand_date <= shopping_range.end_date,
        )
        .order_by(ProductionRequirement.demand_date.asc(), Product.name.asc())
    ).all()
    incomplete = [
        IncompleteRecipeNotice(
            requirement_id=req_id,
            product_id=product_id,
            product_name=product_name,
            demand_date=demand_date,
            demand_quantity=demand_quantity,
        )
        for req_id, product_id, product_name, demand_date, demand_quantity in incomplete_rows
    ]

    return ShoppingListReport(
        range=shopping_range,
        shortages=shortages,
        covered=covered,
        incomplete_recipe=incomplete,
    )


def get_shopping_list(
    db: Session,
    business: Business,
    *,
    business_today: date,
    horizon_days: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> ShoppingListReport:
    """Resolves/validates the requested range (approved Phase 9 rules: a 3|7 preset, the
    Business default when omitted, or a custom range with `start >= today`, `end >=
    start`, at most 90 inclusive calendar days), translating any violation into the
    project's structured `422`, then builds the report. Purely read-only."""
    shopping_range, issues = shopping_domain.resolve_shopping_range(
        business_today=business_today,
        default_horizon_days=business.shopping_horizon_days,
        horizon_days=horizon_days,
        start_date=start_date,
        end_date=end_date,
    )
    if shopping_range is None:
        raise ApiError(
            422,
            "SHOPPING_LIST_RANGE_INVALID",
            "The requested Shopping List range is not valid.",
            issues=[
                {
                    "severity": "ERROR",
                    "code": issue.code,
                    "message": issue.message,
                    "field": issue.field,
                    "resource": "shopping_list",
                    "details": {},
                }
                for issue in issues
            ],
        )
    return build_shopping_list_report(
        db, business, business_today=business_today, shopping_range=shopping_range
    )
