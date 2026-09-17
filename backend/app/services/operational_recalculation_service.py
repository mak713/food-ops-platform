"""The Operational Recalculation Service (Spec §11.4; Phase 7 Plan v2 §5/§8, corrected
by the Final Pre-Implementation Amendment and the Final Architecture Lock).

`recalculate_product_closure` is the non-committing core shared by every Phase 7
write workflow (confirm, confirmed edit, cancel, Recipe migration) and by Draft
Operational Preview: it mutates the SQLAlchemy session (adds/updates/deletes ORM
objects for `ProductionRequirement`/`-Order`/`ProductionIngredientRequirement`/
`IngredientReservation`/`SurplusAllocation`/`PurchasedProductReservation`) but never
calls `db.commit()` — callers compose it into their own single transaction
(Final Architecture Lock §A).

**Caller responsibilities** (this module assumes, never re-acquires):
- The Product, Recipe (if any), every relevant Ingredient, every relevant
  `SurplusInventory` lot, and the `PurchasedProductInventory` row (if any) are already
  locked (`SELECT ... FOR UPDATE`) per the composed lock order (Plan v2 §7, corrected
  by Final Pre-Implementation Amendment §5 and Final Architecture Lock §B).
- Any lifecycle mutation this recalculation depends on seeing (a just-confirmed
  Order's status, a just-created RecipeRevision) has already been `db.flush()`-ed,
  never merely assigned in memory (Final Architecture Lock §A).

**Active-Production-Run protection** (Plan v2 §6, sharpened per the mid-implementation
directive): a `ProductionRequirement` currently referenced by an `IN_PRODUCTION`
`ProductionRun` (`ProductionRun.source_production_requirement_id`) is *entirely*
exempt from this pass — never recomputed, never deleted, never re-linked. Deleting
such a row would `SET NULL` the run's own `source_production_requirement_id` FK,
silently destroying the active-run protection this same module (and the write-time
precondition check) depends on. Every other (non-protected, "rebuildable")
`ProductionRequirement` row for this Product is deleted and rebuilt fresh by this
pass — a full recompute-and-replace, never an incremental patch, matching PLAN-010's
"replace/reconcile ... rather than duplicate."
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import CalculationStatus, OrderStatus, ProductionRunStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order, OrderLine
from app.db.models.product import Product
from app.db.models.production import (
    IngredientReservation,
    ProductionIngredientRequirement,
    ProductionRequirement,
    ProductionRequirementOrder,
    ProductionRun,
)
from app.db.models.purchased_inventory import PurchasedProductInventory
from app.db.models.recipe import Recipe, RecipeRevision, RecipeRevisionIngredient
from app.db.models.surplus import PurchasedProductReservation, SurplusAllocation, SurplusInventory
from app.domain import demand_aggregation, production_costing, recipe_scaling
from app.domain import ingredient_requirements as ingredient_requirements_domain
from app.domain import surplus_allocation as surplus_allocation_domain
from app.domain.inventory_costing import is_representable_in_numeric_18_6, quantize_for_storage
from app.domain.representability import is_representable_in_int32

_OVERFLOW_CODE = "PRODUCTION_REQUIREMENT_VALUE_OVERFLOW"
_CONTEXT_PRECISION = 50
"""Matches the isolation precision every `app/domain/` Phase 7 calculator uses
(ADR-109) — the handful of Decimal accumulations that unavoidably live at this
service layer (summing already-isolated per-ingredient costs/quantities) run under
this same explicit, fixed-precision context rather than the caller's ambient one."""


def _overflow_error(field: str) -> ApiError:
    """Service-layer translation of a representability failure into the shared,
    structured Phase 7 error code (Final Architecture Lock §G) — never raised from
    `app/domain/`."""
    return ApiError(
        422,
        _OVERFLOW_CODE,
        "A derived operational value could not be represented for storage.",
        issues=[
            {
                "severity": "ERROR",
                "code": _OVERFLOW_CODE,
                "message": f"{field} is too large to store.",
                "field": field,
                "resource": "production_requirement",
                "details": {},
            }
        ],
    )


_QUANTITY_TOO_SMALL_CODE = "QUANTITY_TOO_SMALL"


def _quantity_too_small_error(field: str) -> ApiError:
    """Phase 7 Final Semantic & Precision Correction Plan, Finding 2 — reuses the
    exact error code and message shape already established for this identical
    concern in `ingredient_inventory_service._require_positive_quantity_survived_
    rounding` (Phase 5 correction-pass finding 4, item 1): a raw value that is
    genuinely positive can still collapse to `0.000000` once rounded to the
    stored 6-decimal precision (e.g. converting a small Recipe-line unit into an
    Ingredient's own, freely-chosen, larger `canonical_unit`) — never silently
    become a zero-quantity row, and never surface as a raw DB CHECK-constraint
    `IntegrityError`."""
    return ApiError(
        422,
        _QUANTITY_TOO_SMALL_CODE,
        f"This {field} is too small to be represented at the required precision "
        "once converted to the ingredient's canonical unit.",
    )


def _persist_numeric_18_6(value: Decimal, *, field: str, require_positive: bool = False) -> Decimal:
    quantized = quantize_for_storage(value)
    if require_positive and value > 0 and quantized == 0:
        raise _quantity_too_small_error(field)
    if not is_representable_in_numeric_18_6(quantized):
        raise _overflow_error(field)
    return quantized


def _persist_optional_numeric_18_6(value: Decimal | None, *, field: str) -> Decimal | None:
    if value is None:
        return None
    return _persist_numeric_18_6(value, field=field)


def _persist_int32(value: int, *, field: str) -> int:
    if not is_representable_in_int32(value):
        raise _overflow_error(field)
    return value


def _local_time_invalid_error(reason: str) -> ApiError:
    """Final Pre-Implementation Amendment §2's deterministic DST guard — a
    nonexistent (spring-forward gap) or ambiguous (fall-back overlap) Business-local
    fulfillment wall-clock time is rejected rather than silently resolved."""
    return ApiError(
        422,
        "FULFILLMENT_LOCAL_TIME_INVALID",
        "This fulfillment date/time does not correspond to a valid, unambiguous "
        "moment in the Business's local timezone.",
        issues=[
            {
                "severity": "ERROR",
                "code": "FULFILLMENT_LOCAL_TIME_INVALID",
                "message": (
                    "This local fulfillment time does not exist."
                    if reason == "nonexistent"
                    else "This local fulfillment time is ambiguous."
                ),
                "field": "fulfillment_time",
                "resource": "production_requirement",
                "details": {"reason": reason},
            }
        ],
    )


def _persist_suggested_start(
    result: production_costing.SuggestedStartResult,
) -> datetime | None:
    """Service-layer translation of `calculate_suggested_start`'s structured,
    non-raising result (Final Architecture Lock §G — the pure domain layer never
    raises `ApiError`). `MISSING_INPUT` is a normal, non-error `NULL` outcome;
    `NONEXISTENT_LOCAL_TIME`/`AMBIGUOUS_LOCAL_TIME` reject the whole operation;
    `ARITHMETIC_OVERFLOW` (all inputs present and individually valid, but the
    derived duration overflows `datetime` arithmetic) is a data-integrity anomaly,
    never silently degraded to `NULL` (Amendment §3)."""
    status = production_costing.SuggestedStartStatus
    if result.status is status.OK:
        return result.suggested_start_at_utc
    if result.status is status.MISSING_INPUT:
        return None
    if result.status is status.NONEXISTENT_LOCAL_TIME:
        raise _local_time_invalid_error("nonexistent")
    if result.status is status.AMBIGUOUS_LOCAL_TIME:
        raise _local_time_invalid_error("ambiguous")
    raise _overflow_error("suggested_start_at")


def validate_fulfillment_local_time(
    business: Business, fulfillment_date: date | None, fulfillment_time: time | None
) -> None:
    """Phase 7 Final Lifecycle Invariant Correction Plan, Finding C — validates a
    Business-local fulfillment wall-clock moment independent of Recipe/elapsed-time
    availability or Product-type composition (Produced/Purchased/Custom Item alike).
    No-op when either field is missing — a missing date/time is a separate,
    already-covered concern (structural-confirmation-readiness / the
    `MISSING_FULFILLMENT_TIME` warning), not this function's job. Raises the same
    `FULFILLMENT_LOCAL_TIME_INVALID` structured error the production-timing layer
    already establishes for a nonexistent/ambiguous local moment."""
    if fulfillment_date is None or fulfillment_time is None:
        return
    validity = production_costing.validate_business_local_datetime(
        local_date=fulfillment_date,
        local_time=fulfillment_time,
        business_timezone=business.timezone,
    )
    if validity is production_costing.LocalTimeValidity.NONEXISTENT:
        raise _local_time_invalid_error("nonexistent")
    if validity is production_costing.LocalTimeValidity.AMBIGUOUS:
        raise _local_time_invalid_error("ambiguous")


@dataclass(frozen=True)
class HypotheticalLine:
    """One proposed-but-unpersisted demand line, supplied only by Draft Operational
    Preview (Plan v2 §12, corrected by Amendment §4) — never used by a real commit
    path. `order_line_id` may be a synthetic id for a brand-new proposed line."""

    order_id: uuid.UUID
    order_line_id: uuid.UUID
    product_id: uuid.UUID
    underlying_quantity: Decimal
    fulfillment_date: date
    fulfillment_time: time | None


@dataclass(frozen=True)
class IngredientShortageResult:
    ingredient_id: uuid.UUID
    shortage_quantity: Decimal
    physical_quantity: Decimal
    """The Ingredient's current physical stock — read alongside the shortage
    computation, exposed so Preview (Finding 2) can show current physical inventory
    without a second query of its own."""
    ingredient_name: str
    canonical_unit: str
    """Manual Acceptance UX Correction Plan, Finding 1/2 — both construction sites
    already load a tenant-scoped `Ingredient` row to read `physical_quantity`; these
    two fields are read off that same row at zero extra queries, so the Ingredient
    availability display and the shortage warning message can identify the
    Ingredient by name/unit instead of only its opaque id."""


@dataclass(frozen=True)
class ProductionRequirementSummary:
    """One (rebuilt or protected) `ProductionRequirement` group's full operational
    detail, surfaced to both the real write path's downstream callers and Draft
    Operational Preview (Phase 7 Implementation Remediation Plan, Finding 2/4/5) —
    the seller-facing production/batch/workload/timing/cost picture, not merely a
    financial total. `is_protected` distinguishes a row this pass left untouched
    (an active-`IN_PRODUCTION`-run-covered group) from one it just (re)computed."""

    requirement_id: uuid.UUID
    product_id: uuid.UUID
    recipe_revision_id: uuid.UUID | None
    demand_date: date
    is_protected: bool
    missing_recipe: bool
    confirmed_demand_quantity: Decimal
    surplus_allocated_quantity: Decimal
    production_demand_quantity: Decimal
    recommended_batches: int | None
    expected_output_quantity: Decimal | None
    expected_excess_quantity: Decimal | None
    estimated_active_minutes: int | None
    estimated_elapsed_minutes: int | None
    estimated_ingredient_cost: Decimal | None
    estimated_labor_cost: Decimal | None
    estimated_direct_production_cost: Decimal | None
    suggested_start_at: datetime | None


@dataclass(frozen=True)
class RecalcResult:
    product_id: uuid.UUID
    missing_recipe: bool
    """True when this Product's rebuildable confirmed demand includes at least one
    group with no applicable RecipeRevision — surfaced as a WARNING even when Surplus
    fully covers that demand (Plan v2 §10, corrected by Amendment §10)."""
    ingredient_shortages: list[IngredientShortageResult]
    purchased_shortage_quantity: Decimal | None
    """`None` when this Product is not PURCHASED-type."""
    requirement_ids: list[uuid.UUID]
    """Every (rebuilt or protected) ProductionRequirement id now current for this
    Product, for the caller's own downstream bookkeeping (e.g. production-lock
    read-model queries)."""
    requirements: list[ProductionRequirementSummary]
    """Full per-group operational detail for every (rebuilt or protected)
    requirement now current for this Product — the source Preview/confirm/edit
    responses render directly, so the frontend never reconstructs operational math
    of its own (Finding 2)."""
    candidate_ingredient_totals: dict[uuid.UUID, Decimal]
    """This Product's own per-Ingredient candidate (rebuildable-closure) reserved
    quantity for this recalculation pass — exactly the `ingredient_totals` this
    function already computes internally to produce its own (necessarily
    single-Product) `ingredient_shortages` above. Empty for a PURCHASED Product
    (no Ingredient concept). Exposed so a caller recalculating several Products
    together in one Preview/Confirm/Edit request can sum every affected Product's
    own candidate total for a shared Ingredient before reading one authoritative
    combined shortage (Phase 7 Final Remediation Correction Plan, Finding 5) —
    each Product's own `ingredient_shortages` above only ever reflects what THIS
    Product alone contributes, which understates a shortage a sibling Product's
    simultaneous candidate demand on the same Ingredient would also create."""
    rebuildable_requirement_ids: frozenset[uuid.UUID]
    """Every `ProductionRequirement` id for this Product that this pass treated as
    rebuildable (i.e., NOT protected by an active `IN_PRODUCTION` run) — exactly
    `rebuildable_requirement_ids_before`, exposed so a multi-Product caller can
    union every affected Product's own set into one `exclude_requirement_ids`
    argument for `compute_combined_ingredient_shortages` (Finding 5): an active
    reservation belonging to any of these ids is this recalculation's OWN
    candidate state, about to be superseded, and must never double-count as both
    "external" and "candidate" in the combined formula. Empty for a PURCHASED
    Product (no Ingredient reservation concept)."""


def _requirement_summary(
    requirement: ProductionRequirement, *, is_protected: bool
) -> ProductionRequirementSummary:
    return ProductionRequirementSummary(
        requirement_id=requirement.id,
        product_id=requirement.product_id,
        recipe_revision_id=requirement.recipe_revision_id,
        demand_date=requirement.demand_date,
        is_protected=is_protected,
        missing_recipe=requirement.calculation_status == CalculationStatus.INCOMPLETE_RECIPE,
        confirmed_demand_quantity=requirement.confirmed_demand_quantity,
        surplus_allocated_quantity=requirement.surplus_allocated_quantity,
        production_demand_quantity=requirement.production_demand_quantity,
        recommended_batches=requirement.recommended_batches,
        expected_output_quantity=requirement.expected_output_quantity,
        expected_excess_quantity=requirement.expected_excess_quantity,
        estimated_active_minutes=requirement.estimated_active_minutes,
        estimated_elapsed_minutes=requirement.estimated_elapsed_minutes,
        estimated_ingredient_cost=requirement.estimated_ingredient_cost,
        estimated_labor_cost=requirement.estimated_labor_cost,
        estimated_direct_production_cost=requirement.estimated_direct_production_cost,
        suggested_start_at=requirement.suggested_start_at,
    )


def get_protected_requirement_ids(
    db: Session, business: Business, product: Product
) -> set[uuid.UUID]:
    """Every `ProductionRequirement` for this Product currently referenced by an
    `IN_PRODUCTION` `ProductionRun` — entirely exempt from this recalculation pass."""
    rows = db.scalars(
        select(ProductionRequirement.id)
        .join(
            ProductionRun,
            ProductionRun.source_production_requirement_id == ProductionRequirement.id,
        )
        .where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.business_id == business.id,
            ProductionRun.business_id == business.id,
            ProductionRun.status == ProductionRunStatus.IN_PRODUCTION,
        )
    ).all()
    return set(rows)


def get_production_locked_order_line_ids(
    db: Session, business: Business, product: Product
) -> set[uuid.UUID]:
    """Every OrderLine currently linked to a protected `ProductionRequirement` for
    this Product — the conservative "whole line is production-locked" rule (Plan v2
    §6): the frozen schema assigns no per-Order output at Start, so protection is
    all-or-nothing per linked line, not per physical unit."""
    protected_ids = get_protected_requirement_ids(db, business, product)
    if not protected_ids:
        return set()
    rows = db.scalars(
        select(ProductionRequirementOrder.order_line_id).where(
            ProductionRequirementOrder.production_requirement_id.in_(protected_ids),
            ProductionRequirementOrder.business_id == business.id,
        )
    ).all()
    return set(rows)


def revisions_participating_in_closure(
    db: Session, business: Business, product: Product, *, current_revision_id: uuid.UUID | None
) -> set[uuid.UUID]:
    """The union of RecipeRevisions whose ingredients this Product's recalculation
    pass can actually read/write reservations against: the *current* revision (for
    newly-linked demand) plus every distinct revision already referenced by an
    existing `ProductionRequirementOrder` -> `ProductionRequirement` link for this
    Product (retained historical pins — a line kept on a non-current revision by
    `_resolve_pin`, Plan v2 §9). This is a lock-acquisition-only query: an unchanged
    archived/carried-forward revision found here is never subjected to the
    active-state rejection a newly-selected reference would get (ADR-108 carry-
    forward semantics, Final Architecture Lock §B) — it is only ever being locked for
    serialization, never validated.

    Shared between `order_lifecycle_service._acquire_recipe_and_ingredient_locks`
    (confirm/confirmed-edit/cancel's own Recipe/Ingredient lock pass) and
    `recipe_service`'s Recipe-migration Ingredient lock (Phase 7 Final Remediation
    Correction Plan, Finding 4) — both need the identical "every revision this
    Product's closure might still touch" answer, so this lives here rather than
    being duplicated."""
    revision_ids: set[uuid.UUID] = set()
    if current_revision_id is not None:
        revision_ids.add(current_revision_id)
    pinned = db.scalars(
        select(ProductionRequirement.recipe_revision_id)
        .join(
            ProductionRequirementOrder,
            ProductionRequirementOrder.production_requirement_id == ProductionRequirement.id,
        )
        .where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.business_id == business.id,
            ProductionRequirementOrder.business_id == business.id,
            ProductionRequirement.recipe_revision_id.is_not(None),
        )
        .distinct()
    ).all()
    revision_ids |= {r for r in pinned if r is not None}
    return revision_ids


def compute_combined_ingredient_shortages(
    db: Session,
    business: Business,
    ingredient_ids: Iterable[uuid.UUID],
    *,
    exclude_requirement_ids: Iterable[uuid.UUID] = (),
    extra_candidate: dict[uuid.UUID, Decimal] | None = None,
) -> dict[uuid.UUID, IngredientShortageResult]:
    """The single authoritative shortage-per-Ingredient read across MULTIPLE
    Products' recalculation results in one Preview/Confirm/Edit request (Phase 7
    Final Remediation Correction Plan, Finding 5; Final Architecture Lock §E,
    extended to the multi-Product case): `recalculate_product_closure` (via
    `_recalculate_produced_closure`) already computes a fully correct shortage for
    a shared Ingredient WITHIN one Product's own pass — its formula there reads
    every *other* Product's already-persisted reservations as "external." But when
    several Products in the SAME request each contribute a *candidate* (not yet
    persisted, or independently `persist=False`) reservation to the same shared
    Ingredient, each Product's own independent call can only ever see its own
    candidate — never a sibling Product's — so summing each Product's own
    `ingredient_shortages` entry for that Ingredient understates the true combined
    demand (the documented 100-physical/60/60 example: each call independently
    sees 60 < 100 and reports zero, when the true combined demand is 120, a real
    shortage of 20).

    `external = SUM(active IngredientReservation.quantity_canonical for this
    Ingredient) EXCLUDING any reservation whose production_requirement_id is in
    `exclude_requirement_ids`` (the union of every affected Product's own
    `RecalcResult.rebuildable_requirement_ids` — each such reservation is this
    same combined pass's OWN prior candidate state, about to be superseded, never
    counted as "external"). `total = external + extra_candidate.get(ingredient_id,
    0)` (the union-summed `RecalcResult.candidate_ingredient_totals` across every
    affected Product). `shortage = max(0, total - physical_quantity)`. Isolated in
    its own `decimal.localcontext()` (ADR-109/Finding 9), matching the identical
    per-Ingredient computation `_recalculate_produced_closure` already performs
    for the single-Product case — this is that same formula, generalized to a
    caller-supplied combined candidate/exclusion set instead of one Product's own
    internally-computed values.

    Returns `{ingredient_id: IngredientShortageResult}` — an id absent from
    `ingredient_ids` or not found for this tenant is simply absent from the
    result, never a zero-filled placeholder."""
    extra_candidate = extra_candidate or {}
    exclude_ids = set(exclude_requirement_ids)
    ids = sorted(set(ingredient_ids))
    results: dict[uuid.UUID, IngredientShortageResult] = {}
    if not ids:
        return results

    ingredients = {
        row.id: row
        for row in db.scalars(
            select(Ingredient).where(Ingredient.id.in_(ids), Ingredient.business_id == business.id)
        )
    }
    for ingredient_id in ids:
        ingredient = ingredients.get(ingredient_id)
        if ingredient is None:
            continue
        conditions = [
            IngredientReservation.ingredient_id == ingredient_id,
            IngredientReservation.business_id == business.id,
        ]
        if exclude_ids:
            conditions.append(IngredientReservation.production_requirement_id.not_in(exclude_ids))
        external_reserved = db.scalar(
            select(func.coalesce(func.sum(IngredientReservation.quantity_canonical), 0)).where(
                *conditions
            )
        ) or Decimal(0)
        with decimal.localcontext() as ctx:
            ctx.prec = _CONTEXT_PRECISION
            total_reserved = external_reserved + extra_candidate.get(ingredient_id, Decimal(0))
        shortage = demand_aggregation.calculate_shortage(
            total_reserved, ingredient.physical_quantity
        )
        results[ingredient_id] = IngredientShortageResult(
            ingredient_id,
            shortage,
            ingredient.physical_quantity,
            ingredient.name,
            ingredient.canonical_unit,
        )
    return results


def _gather_produced_demand(
    db: Session,
    business: Business,
    product: Product,
    *,
    exclude_order_id: uuid.UUID | None,
    hypothetical_lines: Sequence[HypotheticalLine],
) -> list[tuple[OrderLine, Order]]:
    """Every CONFIRMED OrderLine referencing this Product, at any demand_date (Plan v2
    §2/§8 — the 'today forward' filter is deliberately absent). Preview's
    `exclude_order_id` removes that Order's own persisted contribution first (Amendment
    §4), so its hypothetical replacement lines are the only representation of that
    Order's demand for this computation."""
    conditions = [
        OrderLine.product_id == product.id,
        OrderLine.business_id == business.id,
        Order.business_id == business.id,
        Order.status == OrderStatus.CONFIRMED,
    ]
    if exclude_order_id is not None:
        conditions.append(Order.id != exclude_order_id)
    rows = db.execute(select(OrderLine, Order).join(Order).where(*conditions)).all()
    real_lines = [(line, order) for line, order in rows]
    return real_lines


def _resolve_current_revision(
    db: Session, business: Business, product: Product
) -> RecipeRevision | None:
    recipe = db.scalar(
        select(Recipe).where(Recipe.product_id == product.id, Recipe.business_id == business.id)
    )
    if recipe is None:
        return None
    return db.scalar(
        select(RecipeRevision).where(
            RecipeRevision.recipe_id == recipe.id,
            RecipeRevision.business_id == business.id,
            RecipeRevision.is_current.is_(True),
        )
    )


def _resolve_pin(
    db: Session,
    business: Business,
    product: Product,
    order_line_id: uuid.UUID,
    *,
    current_revision: RecipeRevision | None,
) -> uuid.UUID | None:
    """A retained line reuses its existing pin (looked up via the still-live
    `ProductionRequirementOrder` link, scoped to THIS Product so a just-changed
    product reference never accidentally reuses a stale pin from its old Product); a
    newly-linked line (no existing link, or a product-identity change just occurred)
    resolves fresh to whatever is currently `is_current` (Plan v2 §9)."""
    existing_link = db.scalar(
        select(ProductionRequirementOrder)
        .join(
            ProductionRequirement,
            ProductionRequirementOrder.production_requirement_id == ProductionRequirement.id,
        )
        .where(
            ProductionRequirementOrder.order_line_id == order_line_id,
            ProductionRequirementOrder.business_id == business.id,
            ProductionRequirement.product_id == product.id,
        )
    )
    if existing_link is not None:
        requirement = db.scalar(
            select(ProductionRequirement).where(
                ProductionRequirement.id == existing_link.production_requirement_id,
                ProductionRequirement.business_id == business.id,
            )
        )
        return requirement.recipe_revision_id
    return current_revision.id if current_revision is not None else None


def recalculate_product_closure(
    db: Session,
    business: Business,
    product: Product,
    *,
    business_today: date,
    exclude_order_id: uuid.UUID | None = None,
    hypothetical_lines: Sequence[HypotheticalLine] = (),
    persist: bool = True,
) -> RecalcResult:
    """The recalculation core. `persist=False` (Draft Operational Preview only) skips
    every session mutation and returns the same computed result a real commit would —
    Preview and Commit share this exact function (Spec §11.5)."""
    protected_requirement_ids = get_protected_requirement_ids(db, business, product)

    if product.product_type == ProductType.PURCHASED:
        return _recalculate_purchased_closure(
            db,
            business,
            product,
            exclude_order_id=exclude_order_id,
            hypothetical_lines=hypothetical_lines,
            protected_requirement_ids=protected_requirement_ids,
            persist=persist,
        )

    return _recalculate_produced_closure(
        db,
        business,
        product,
        business_today=business_today,
        exclude_order_id=exclude_order_id,
        hypothetical_lines=hypothetical_lines,
        protected_requirement_ids=protected_requirement_ids,
        persist=persist,
    )


def _recalculate_purchased_closure(
    db: Session,
    business: Business,
    product: Product,
    *,
    exclude_order_id: uuid.UUID | None,
    hypothetical_lines: Sequence[HypotheticalLine],
    protected_requirement_ids: set[uuid.UUID],
    persist: bool,
) -> RecalcResult:
    """Purchased-Product demand: no whole-batch/ingredient/surplus concept — a
    reservation is 1:1 with each line's own `underlying_quantity`, and shortage is
    `physical - active reservations` (Spec §7.3/§4.4). No fake
    `PurchasedProductInventory` row is ever created merely to reserve or lock
    (Final Pre-Implementation Amendment §13) — a missing row means zero physical
    stock for the shortage calculation."""
    real_lines = _gather_produced_demand(
        db, business, product, exclude_order_id=exclude_order_id, hypothetical_lines=()
    )
    # Purchased reservations have no analogous "protected by an IN_PRODUCTION run"
    # concept (Phase 8 never runs Purchased-goods through ProductionRun) — every
    # existing reservation for this Product is rebuildable.
    del protected_requirement_ids

    if persist:
        db.query(PurchasedProductReservation).filter(
            PurchasedProductReservation.product_id == product.id,
            PurchasedProductReservation.business_id == business.id,
        ).delete(synchronize_session=False)

    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        total_reserved = Decimal(0)
        for line, order in real_lines:
            quantity = _persist_numeric_18_6(
                line.underlying_quantity, field="purchased_reservation_quantity"
            )
            total_reserved += quantity
            if persist:
                db.add(
                    PurchasedProductReservation(
                        id=uuid.uuid4(),
                        business_id=business.id,
                        product_id=product.id,
                        order_id=order.id,
                        order_line_id=line.id,
                        quantity=quantity,
                    )
                )
        for hyp in hypothetical_lines:
            if hyp.product_id != product.id:
                continue
            total_reserved += _persist_numeric_18_6(
                hyp.underlying_quantity, field="purchased_reservation_quantity"
            )

    inventory_row = db.scalar(
        select(PurchasedProductInventory).where(
            PurchasedProductInventory.product_id == product.id,
            PurchasedProductInventory.business_id == business.id,
        )
    )
    physical_quantity = inventory_row.physical_quantity if inventory_row is not None else Decimal(0)
    shortage = demand_aggregation.calculate_shortage(total_reserved, physical_quantity)

    return RecalcResult(
        product_id=product.id,
        missing_recipe=False,
        ingredient_shortages=[],
        purchased_shortage_quantity=shortage,
        requirement_ids=[],
        requirements=[],
        candidate_ingredient_totals={},
        rebuildable_requirement_ids=frozenset(),
    )


def _recalculate_produced_closure(
    db: Session,
    business: Business,
    product: Product,
    *,
    business_today: date,
    exclude_order_id: uuid.UUID | None,
    hypothetical_lines: Sequence[HypotheticalLine],
    protected_requirement_ids: set[uuid.UUID],
    persist: bool,
) -> RecalcResult:
    real_lines = _gather_produced_demand(
        db,
        business,
        product,
        exclude_order_id=exclude_order_id,
        hypothetical_lines=hypothetical_lines,
    )
    current_revision = _resolve_current_revision(db, business, product)

    # --- Pin resolution + protected/rebuildable split -----------------------------
    protected_line_ids = get_production_locked_order_line_ids(db, business, product)

    rebuildable_demand: list[demand_aggregation.ConfirmedProducedDemandLine] = []
    line_lookup: dict[uuid.UUID, tuple[OrderLine, Order]] = {}
    for line, order in real_lines:
        if line.id in protected_line_ids:
            continue  # entirely untouched by this pass
        pin = _resolve_pin(db, business, product, line.id, current_revision=current_revision)
        rebuildable_demand.append(
            demand_aggregation.ConfirmedProducedDemandLine(
                order_line_id=line.id,
                product_id=product.id,
                recipe_revision_id=pin,
                demand_date=order.fulfillment_date,
                quantity=line.underlying_quantity,
            )
        )
        line_lookup[line.id] = (line, order)

    for hyp in hypothetical_lines:
        if hyp.product_id != product.id:
            continue
        # Amendment §4 (CONFIRMED-Order Preview substitution): a hypothetical line
        # standing in for a RETAINED existing OrderLine (same, stable order_line_id,
        # Preview passes the real id through rather than a synthetic one — see
        # `order_preview_service.py`) must reuse that line's existing pin exactly as
        # a real edit would, not re-resolve to whatever is current now.
        # `_resolve_pin` already implements exactly this lookup (and correctly falls
        # through to `current_revision` for a genuinely new/synthetic id that no
        # existing link references) — reused unchanged, never a second pin-resolution
        # rule invented here.
        pin = _resolve_pin(
            db, business, product, hyp.order_line_id, current_revision=current_revision
        )
        rebuildable_demand.append(
            demand_aggregation.ConfirmedProducedDemandLine(
                order_line_id=hyp.order_line_id,
                product_id=product.id,
                recipe_revision_id=pin,
                demand_date=hyp.fulfillment_date,
                quantity=hyp.underlying_quantity,
            )
        )

    grouped_demand = demand_aggregation.aggregate_produced_demand(rebuildable_demand)

    # A rebuildable group whose (product, revision, date) key exactly matches an
    # already-protected requirement's own key cannot be written: the DB's own
    # partial unique index allows only one row per key, and a protected row must
    # never be re-linked or overwritten (module docstring). This is a genuine,
    # conservative extension of the same "whole line is production-locked" rule
    # (Plan v2 §6) to the group level — new demand landing on an already-started
    # (product, revision, date) combination is itself rejected, never silently
    # merged or duplicated.
    if protected_requirement_ids:
        protected_keys = {
            (req.product_id, req.recipe_revision_id, req.demand_date)
            for req in db.scalars(
                select(ProductionRequirement).where(
                    ProductionRequirement.id.in_(protected_requirement_ids),
                    ProductionRequirement.business_id == business.id,
                )
            )
        }
        colliding_keys = set(grouped_demand) & protected_keys
        if colliding_keys:
            raise ApiError(
                409,
                "PRODUCTION_LOCKED_DEMAND_DATE_CONFLICT",
                "New demand for this Product/Recipe Revision/date is already covered by "
                "an active production run and cannot be confirmed until that run "
                "completes or is canceled.",
                issues=[
                    {
                        "severity": "ERROR",
                        "code": "PRODUCTION_LOCKED_DEMAND_DATE_CONFLICT",
                        "message": (
                            "This Product's demand for "
                            f"{demand_date.isoformat()} is already covered by an active "
                            "production run."
                        ),
                        "field": "fulfillment_date",
                        "resource": "production_requirement",
                        "details": {"demand_date": demand_date.isoformat()},
                    }
                    for _, _, demand_date in sorted(colliding_keys, key=lambda k: k[2])
                ],
            )

    # Shared order_line_id -> fulfillment_time lookup (real lines via their Order,
    # hypothetical lines via their own field) — reused by both the surplus-priority
    # ordering below and the suggested-start timing step (Finding 4).
    hypothetical_by_line_id = {h.order_line_id: h for h in hypothetical_lines}
    fulfillment_time_by_line_id: dict[uuid.UUID, time | None] = {}
    for item in rebuildable_demand:
        if item.order_line_id in line_lookup:
            fulfillment_time_by_line_id[item.order_line_id] = line_lookup[item.order_line_id][
                1
            ].fulfillment_time
        elif item.order_line_id in hypothetical_by_line_id:
            fulfillment_time_by_line_id[item.order_line_id] = hypothetical_by_line_id[
                item.order_line_id
            ].fulfillment_time

    # --- Surplus allocation (line-level; fixed lot quantity excluded up front) ----
    lots = db.scalars(
        select(SurplusInventory).where(
            SurplusInventory.product_id == product.id,
            SurplusInventory.business_id == business.id,
            SurplusInventory.is_reusable.is_(True),
        )
    ).all()

    fixed_lot_allocations: dict[uuid.UUID, Decimal] = {}
    if protected_requirement_ids:
        rows = db.execute(
            select(SurplusAllocation.surplus_inventory_id, SurplusAllocation.quantity).where(
                SurplusAllocation.production_requirement_id.in_(protected_requirement_ids),
                SurplusAllocation.business_id == business.id,
            )
        ).all()
        with decimal.localcontext() as ctx:
            ctx.prec = _CONTEXT_PRECISION
            for surplus_inventory_id, quantity in rows:
                fixed_lot_allocations[surplus_inventory_id] = (
                    fixed_lot_allocations.get(surplus_inventory_id, Decimal(0)) + quantity
                )

    domain_lots = [
        surplus_allocation_domain.SurplusLot(
            surplus_inventory_id=lot.id,
            product_id=lot.product_id,
            physical_quantity=lot.physical_quantity,
            already_allocated_elsewhere=fixed_lot_allocations.get(lot.id, Decimal(0)),
            usable_through_date=lot.usable_through_date,
            produced_at=lot.produced_at,
        )
        for lot in lots
    ]
    domain_demand_items = [
        surplus_allocation_domain.DemandItem(
            order_id=(
                line_lookup[item.order_line_id][1].id
                if item.order_line_id in line_lookup
                else next(
                    h.order_id for h in hypothetical_lines if h.order_line_id == item.order_line_id
                )
            ),
            order_line_id=item.order_line_id,
            product_id=item.product_id,
            recipe_revision_id=item.recipe_revision_id,
            quantity=item.quantity,
            demand_date=item.demand_date,
            fulfillment_time=fulfillment_time_by_line_id.get(item.order_line_id),
        )
        for item in rebuildable_demand
    ]
    allocation_results = surplus_allocation_domain.allocate_surplus(
        domain_lots, domain_demand_items, today=business_today
    )
    surplus_allocated_by_key: dict[demand_aggregation.DemandKey, Decimal] = {}
    surplus_rows_to_create: list[SurplusAllocation] = []
    demand_line_key: dict[uuid.UUID, demand_aggregation.DemandKey] = {
        item.order_line_id: (item.product_id, item.recipe_revision_id, item.demand_date)
        for item in rebuildable_demand
    }
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        for result in allocation_results:
            key = demand_line_key[result.order_line_id]
            surplus_allocated_by_key[key] = (
                surplus_allocated_by_key.get(key, Decimal(0)) + result.quantity
            )
            if result.order_line_id in line_lookup:
                surplus_rows_to_create.append(
                    SurplusAllocation(
                        id=uuid.uuid4(),
                        business_id=business.id,
                        surplus_inventory_id=result.surplus_inventory_id,
                        # filled in once the requirement row is known
                        production_requirement_id=None,
                        order_id=result.order_id,
                        order_line_id=result.order_line_id,
                        quantity=_persist_numeric_18_6(
                            result.quantity, field="surplus_allocations.quantity"
                        ),
                    )
                )

    # --- Per-(revision, date) group: whole-batch, ingredient requirements, cost ----
    missing_recipe = False
    all_new_requirements: list[ProductionRequirement] = []
    all_new_links: list[ProductionRequirementOrder] = []
    all_new_ingredient_requirements: list[ProductionIngredientRequirement] = []
    ingredient_totals: dict[uuid.UUID, Decimal] = {}

    lines_by_key: dict[demand_aggregation.DemandKey, list[uuid.UUID]] = {}
    for item in rebuildable_demand:
        key = (item.product_id, item.recipe_revision_id, item.demand_date)
        lines_by_key.setdefault(key, []).append(item.order_line_id)

    for key, confirmed_quantity in grouped_demand.items():
        _, revision_id, demand_date = key
        surplus_for_group = surplus_allocated_by_key.get(key, Decimal(0))
        remaining_demand = demand_aggregation.calculate_shortage(
            confirmed_quantity, surplus_for_group
        )

        requirement_id = uuid.uuid4()
        revision = (
            db.scalar(
                select(RecipeRevision).where(
                    RecipeRevision.id == revision_id, RecipeRevision.business_id == business.id
                )
            )
            if revision_id is not None
            else None
        )

        if revision is None:
            missing_recipe = True
            requirement = ProductionRequirement(
                id=requirement_id,
                business_id=business.id,
                product_id=product.id,
                recipe_revision_id=None,
                demand_date=demand_date,
                calculation_status=CalculationStatus.INCOMPLETE_RECIPE,
                confirmed_demand_quantity=_persist_numeric_18_6(
                    confirmed_quantity, field="confirmed_demand_quantity"
                ),
                surplus_allocated_quantity=_persist_numeric_18_6(
                    surplus_for_group, field="surplus_allocated_quantity"
                ),
                production_demand_quantity=_persist_numeric_18_6(
                    remaining_demand, field="production_demand_quantity"
                ),
            )
        else:
            plan = recipe_scaling.calculate_whole_batch_plan(
                remaining_demand, revision.yield_quantity
            )
            batches = _persist_int32(plan.required_batches, field="recommended_batches")

            revision_ingredients = db.scalars(
                select(RecipeRevisionIngredient).where(
                    RecipeRevisionIngredient.recipe_revision_id == revision.id,
                    RecipeRevisionIngredient.business_id == business.id,
                )
            ).all()
            ingredient_lookup = {
                ing.id: ing
                for ing in db.scalars(
                    select(Ingredient).where(
                        Ingredient.id.in_([line.ingredient_id for line in revision_ingredients]),
                        Ingredient.business_id == business.id,
                    )
                )
            }
            domain_lines = [
                ingredient_requirements_domain.RecipeRevisionIngredientLine(
                    ingredient_id=line.ingredient_id,
                    quantity_per_batch=line.quantity,
                    unit=line.unit,
                    ingredient_canonical_unit=ingredient_lookup[line.ingredient_id].canonical_unit,
                )
                for line in revision_ingredients
            ]
            requirements = [
                r
                for r in ingredient_requirements_domain.calculate_ingredient_requirements(
                    domain_lines, batches
                )
                if r.required_quantity_canonical > 0
            ]  # `ck_..._quantity_canonical_positive`/`ck_..._required_qty_canonical_positive`
            # forbid a zero-quantity row (batches == 0 -> nothing to reserve at all).

            workload = production_costing.calculate_workload(
                batches=batches,
                active_minutes_per_batch=revision.active_time_minutes,
                elapsed_minutes_per_batch=revision.elapsed_time_minutes,
            )

            # Suggested-start timing (Spec §14.12; Phase 7 Implementation Remediation
            # Plan, Finding 4; Final Lifecycle Invariant Correction Plan, Finding C):
            # gather every contributing fulfillment time, THEN validate every
            # SUPPLIED (non-None) one for Business-local DST nonexistent/ambiguous
            # state BEFORE branching on whether any contributor is missing a time at
            # all — a missing time on one contributor must never mask a DST-invalid
            # SUPPLIED time on another (the "any None -> NULL" branch below used to
            # short-circuit before any validation ran), and an invalid non-earliest
            # contributor must never hide behind `min()` either. Only once every
            # supplied time is confirmed individually valid do we fall back to the
            # existing missing-time semantics (any contributor lacking a time ->
            # suggested_start_at stays exactly NULL) or select the EARLIEST
            # contributing fulfillment_time on this group's demand_date.
            contributing_line_ids = lines_by_key.get(key, [])
            contributing_times = [
                fulfillment_time_by_line_id.get(line_id) for line_id in contributing_line_ids
            ]
            for candidate_time in contributing_times:
                if candidate_time is None:
                    continue
                candidate_validity = production_costing.validate_business_local_datetime(
                    local_date=demand_date,
                    local_time=candidate_time,
                    business_timezone=business.timezone,
                )
                if candidate_validity is production_costing.LocalTimeValidity.NONEXISTENT:
                    raise _local_time_invalid_error("nonexistent")
                if candidate_validity is production_costing.LocalTimeValidity.AMBIGUOUS:
                    raise _local_time_invalid_error("ambiguous")

            if not contributing_times or any(t is None for t in contributing_times):
                suggested_start_at: datetime | None = None
            else:
                # The earliest (min) element is revalidated a second time inside
                # `calculate_suggested_start` itself — harmless (both checks are pure
                # and cheap), kept for simplicity rather than threading an
                # "already validated" flag through.
                suggested_start_at = _persist_suggested_start(
                    production_costing.calculate_suggested_start(
                        fulfillment_date=demand_date,
                        fulfillment_time=min(contributing_times),
                        elapsed_minutes=workload.estimated_elapsed_minutes,
                        business_timezone=business.timezone,
                    )
                )

            estimated_ingredient_cost: Decimal | None = Decimal(0)
            per_ingredient_costs: dict[uuid.UUID, tuple[Decimal | None, Decimal | None]] = {}
            # Phase 7 Final Semantic & Precision Correction Plan, Findings 2/3: each
            # requirement's would-be-persisted `required_quantity_canonical` — the
            # exact positive-guarded, quantized value `ProductionIngredientRequirement`
            # will actually store below — computed here, ONCE per requirement, so
            # `ingredient_totals` (Finding 3's `candidate_ingredient_totals`) sums
            # the same rounded-per-group value real persistence uses, never the raw
            # domain result. Cost calculation deliberately keeps reading the RAW,
            # unquantized `req.required_quantity_canonical` (unchanged) — quantizing
            # a quantity destined for reservation/requirement storage is a distinct
            # concern from the cost math's own single, separate quantization point.
            quantized_required_by_ingredient: dict[uuid.UUID, Decimal] = {}
            # Every Decimal accumulation below (summing already-isolated per-ingredient
            # costs/quantities) runs inside one explicit, fixed-precision context
            # (ADR-109) rather than the caller's ambient global one.
            with decimal.localcontext() as ctx:
                ctx.prec = _CONTEXT_PRECISION
                for req in requirements:
                    ingredient = ingredient_lookup[req.ingredient_id]
                    quantized_required_by_ingredient[req.ingredient_id] = _persist_numeric_18_6(
                        req.required_quantity_canonical,
                        field="required_quantity_canonical",
                        require_positive=True,
                    )
                    unit_cost = production_costing.resolve_planned_ingredient_unit_cost(
                        physical_quantity=ingredient.physical_quantity,
                        weighted_average_unit_cost=ingredient.weighted_average_unit_cost,
                        replacement_unit_cost=ingredient.replacement_unit_cost,
                        latest_purchase_unit_cost=ingredient.latest_purchase_unit_cost,
                    )
                    if unit_cost is None:
                        per_ingredient_costs[req.ingredient_id] = (None, None)
                        estimated_ingredient_cost = None
                    else:
                        total_cost = production_costing.calculate_ingredient_planned_total_cost(
                            required_quantity_canonical=req.required_quantity_canonical,
                            unit_cost=unit_cost,
                        )
                        per_ingredient_costs[req.ingredient_id] = (unit_cost, total_cost)
                        if estimated_ingredient_cost is not None:
                            estimated_ingredient_cost += total_cost
                    ingredient_totals[req.ingredient_id] = (
                        ingredient_totals.get(req.ingredient_id, Decimal(0))
                        + quantized_required_by_ingredient[req.ingredient_id]
                    )

                labor_cost = production_costing.calculate_planned_labor_cost(
                    batches=batches,
                    active_minutes_per_batch=revision.active_time_minutes,
                    labor_rate=business.default_labor_rate,
                )
                direct_production_cost = (
                    None
                    if estimated_ingredient_cost is None
                    else estimated_ingredient_cost + labor_cost
                )

            requirement = ProductionRequirement(
                id=requirement_id,
                business_id=business.id,
                product_id=product.id,
                recipe_revision_id=revision.id,
                demand_date=demand_date,
                calculation_status=CalculationStatus.CALCULATED,
                confirmed_demand_quantity=_persist_numeric_18_6(
                    confirmed_quantity, field="confirmed_demand_quantity"
                ),
                surplus_allocated_quantity=_persist_numeric_18_6(
                    surplus_for_group, field="surplus_allocated_quantity"
                ),
                production_demand_quantity=_persist_numeric_18_6(
                    remaining_demand, field="production_demand_quantity"
                ),
                recommended_batches=batches,
                expected_output_quantity=_persist_numeric_18_6(
                    plan.expected_output, field="expected_output_quantity"
                ),
                expected_excess_quantity=_persist_numeric_18_6(
                    plan.expected_excess, field="expected_excess_quantity"
                ),
                estimated_active_minutes=_persist_int32(
                    workload.estimated_active_minutes, field="estimated_active_minutes"
                ),
                estimated_elapsed_minutes=(
                    _persist_int32(
                        workload.estimated_elapsed_minutes, field="estimated_elapsed_minutes"
                    )
                    if workload.estimated_elapsed_minutes is not None
                    else None
                ),
                estimated_ingredient_cost=_persist_optional_numeric_18_6(
                    estimated_ingredient_cost, field="estimated_ingredient_cost"
                ),
                estimated_labor_cost=_persist_numeric_18_6(
                    labor_cost, field="estimated_labor_cost"
                ),
                estimated_direct_production_cost=_persist_optional_numeric_18_6(
                    direct_production_cost, field="estimated_direct_production_cost"
                ),
                suggested_start_at=suggested_start_at,
            )
            for req in requirements:
                unit_cost, total_cost = per_ingredient_costs[req.ingredient_id]
                all_new_ingredient_requirements.append(
                    ProductionIngredientRequirement(
                        id=uuid.uuid4(),
                        business_id=business.id,
                        production_requirement_id=requirement_id,
                        ingredient_id=req.ingredient_id,
                        # Already computed and positive-guarded above, alongside the
                        # `ingredient_totals` accumulation (Findings 2/3) — reused
                        # directly rather than re-quantized/re-checked a second time.
                        required_quantity_canonical=quantized_required_by_ingredient[
                            req.ingredient_id
                        ],
                        estimated_unit_cost=_persist_optional_numeric_18_6(
                            unit_cost, field="estimated_unit_cost"
                        ),
                        estimated_total_cost=_persist_optional_numeric_18_6(
                            total_cost, field="estimated_total_cost"
                        ),
                    )
                )

        all_new_requirements.append(requirement)
        for order_line_id in lines_by_key[key]:
            if order_line_id not in line_lookup:
                continue  # a hypothetical (Preview-only) line — never persisted
            line, order = line_lookup[order_line_id]
            all_new_links.append(
                ProductionRequirementOrder(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    production_requirement_id=requirement_id,
                    order_id=order.id,
                    order_line_id=order_line_id,
                    demand_quantity=_persist_numeric_18_6(
                        line.underlying_quantity,
                        field="production_requirement_orders.demand_quantity",
                    ),
                )
            )
        for surplus_row in surplus_rows_to_create:
            if lines_by_key[key] and surplus_row.order_line_id in lines_by_key[key]:
                surplus_row.production_requirement_id = requirement_id

    # --- Ingredient reservation reconciliation + cross-Product shortage ----------
    ingredient_shortages: list[IngredientShortageResult] = []
    all_new_reservations: list[IngredientReservation] = []
    rebuildable_requirement_ids_before = {
        row
        for row in db.scalars(
            select(ProductionRequirement.id).where(
                ProductionRequirement.product_id == product.id,
                ProductionRequirement.business_id == business.id,
                ProductionRequirement.id.not_in(protected_requirement_ids)
                if protected_requirement_ids
                else True,
            )
        )
    }
    for ingredient_id, candidate_reserved in ingredient_totals.items():
        conditions = [
            IngredientReservation.ingredient_id == ingredient_id,
            IngredientReservation.business_id == business.id,
        ]
        if rebuildable_requirement_ids_before:
            conditions.append(
                IngredientReservation.production_requirement_id.not_in(
                    rebuildable_requirement_ids_before
                )
            )
        external_or_fixed_reserved = db.scalar(
            select(func.coalesce(func.sum(IngredientReservation.quantity_canonical), 0)).where(
                *conditions
            )
        ) or Decimal(0)
        ingredient = db.scalar(
            select(Ingredient).where(
                Ingredient.id == ingredient_id, Ingredient.business_id == business.id
            )
        )
        with decimal.localcontext() as ctx:
            ctx.prec = _CONTEXT_PRECISION
            total_reserved = external_or_fixed_reserved + candidate_reserved
        shortage = demand_aggregation.calculate_shortage(
            total_reserved, ingredient.physical_quantity
        )
        ingredient_shortages.append(
            IngredientShortageResult(
                ingredient_id,
                shortage,
                ingredient.physical_quantity,
                ingredient.name,
                ingredient.canonical_unit,
            )
        )

    for requirement in all_new_requirements:
        for ing_req in all_new_ingredient_requirements:
            if ing_req.production_requirement_id != requirement.id:
                continue
            all_new_reservations.append(
                IngredientReservation(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    production_requirement_id=requirement.id,
                    ingredient_id=ing_req.ingredient_id,
                    quantity_canonical=ing_req.required_quantity_canonical,
                )
            )

    # --- Persist: delete every rebuildable row for this Product, insert fresh -----
    if persist:
        if rebuildable_requirement_ids_before:
            db.query(SurplusAllocation).filter(
                SurplusAllocation.production_requirement_id.in_(rebuildable_requirement_ids_before),
                SurplusAllocation.business_id == business.id,
            ).delete(synchronize_session=False)
            db.query(IngredientReservation).filter(
                IngredientReservation.production_requirement_id.in_(
                    rebuildable_requirement_ids_before
                ),
                IngredientReservation.business_id == business.id,
            ).delete(synchronize_session=False)
            db.query(ProductionIngredientRequirement).filter(
                ProductionIngredientRequirement.production_requirement_id.in_(
                    rebuildable_requirement_ids_before
                ),
                ProductionIngredientRequirement.business_id == business.id,
            ).delete(synchronize_session=False)
            db.query(ProductionRequirementOrder).filter(
                ProductionRequirementOrder.production_requirement_id.in_(
                    rebuildable_requirement_ids_before
                ),
                ProductionRequirementOrder.business_id == business.id,
            ).delete(synchronize_session=False)
            db.query(ProductionRequirement).filter(
                ProductionRequirement.id.in_(rebuildable_requirement_ids_before),
                ProductionRequirement.business_id == business.id,
            ).delete(synchronize_session=False)

        for requirement in all_new_requirements:
            db.add(requirement)
        for link in all_new_links:
            db.add(link)
        for ing_req in all_new_ingredient_requirements:
            db.add(ing_req)
        for reservation in all_new_reservations:
            db.add(reservation)
        for surplus_row in surplus_rows_to_create:
            if surplus_row.production_requirement_id is not None:
                db.add(surplus_row)
        db.flush()

    protected_requirement_rows = (
        list(
            db.scalars(
                select(ProductionRequirement).where(
                    ProductionRequirement.id.in_(protected_requirement_ids),
                    ProductionRequirement.business_id == business.id,
                )
            )
        )
        if protected_requirement_ids
        else []
    )
    requirement_summaries = [
        _requirement_summary(r, is_protected=False) for r in all_new_requirements
    ] + [_requirement_summary(r, is_protected=True) for r in protected_requirement_rows]

    return RecalcResult(
        product_id=product.id,
        missing_recipe=missing_recipe,
        ingredient_shortages=ingredient_shortages,
        purchased_shortage_quantity=None,
        requirement_ids=[r.id for r in all_new_requirements] + sorted(protected_requirement_ids),
        requirements=requirement_summaries,
        candidate_ingredient_totals=dict(ingredient_totals),
        rebuildable_requirement_ids=frozenset(rebuildable_requirement_ids_before),
    )
