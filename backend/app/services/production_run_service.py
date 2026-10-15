"""Production Run lifecycle: Start, Cancel, Finish (RUN-001..013; Phase 8 Final
Architecture Lock / Final Architecture Amendment / Final Architecture Lock Addendum).

Mirrors `order_lifecycle_service.py`'s shape exactly: each public function here is the
single transaction owner for its workflow — it locks (per the composed hierarchy in
`operational_recalculation_service`'s own module docstring, extended by Phase 8:
`ProductionRun -> Order -> Customer -> Product -> ProductionRequirement ->
SellingOption -> Recipe -> Ingredient -> Surplus/Purchased inventory`), validates,
mutates, `flush()`-es, optionally recalculates (via `operational_recalculation_service`,
non-committing), and commits exactly once — never a commit followed by a separate
recalculation call.
"""

from __future__ import annotations

import dataclasses
import decimal
import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.tenant import NotFoundError, check_version, commit_or_raise_stale, get_owned_or_404
from app.db.enums import (
    CalculationStatus,
    CostBasisSource,
    CostType,
    InventoryTransactionType,
    ProductionRunStatus,
    SurplusTransactionType,
)
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation
from app.db.models.ingredient import Ingredient, InventoryTransaction
from app.db.models.order import Order
from app.db.models.production import (
    ProductionRequirement,
    ProductionRun,
    ProductionRunIngredient,
    ProductionRunOrderAllocation,
)
from app.db.models.recipe import RecipeRevision, RecipeRevisionIngredient
from app.db.models.surplus import SurplusAllocation, SurplusInventory, SurplusTransaction
from app.domain import ingredient_requirements as ingredient_requirements_domain
from app.domain import production_costing, recipe_scaling
from app.domain.inventory_costing import (
    is_representable_in_numeric_18_6,
    quantize_for_storage,
    resolve_effective_replacement_cost,
)
from app.domain.order_pricing import is_representable_in_numeric_14_2, quantize_money
from app.domain.production_cost_apportionment import CostRecipient, apportion_production_cost
from app.domain.production_output_allocation import (
    OutstandingDemandLine,
    allocate_production_output,
)
from app.domain.representability import is_representable_in_int32
from app.services import (
    ingredient_service,
    order_lifecycle_service,
    product_service,
    readiness_service,
)
from app.services.operational_recalculation_service import RecalcResult, recalculate_product_closure

_COST_BASIS_UNKNOWN_CODE = "PRODUCTION_COST_BASIS_UNKNOWN"
_REQUIREMENT_CHANGED_CODE = "PRODUCTION_REQUIREMENT_CHANGED"
_ALREADY_STARTED_CODE = "PRODUCTION_ALREADY_STARTED"
_ACKNOWLEDGMENT_REQUIRED_CODE = "UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED"
_OVERFLOW_CODE = "PRODUCTION_RUN_VALUE_OVERFLOW"
_CONTEXT_PRECISION = 50
"""Matches the isolation precision every `app/domain/` Phase 7/8 calculator uses
(ADR-109) — the handful of Decimal accumulations/divisions that unavoidably live at
this service layer run under this same explicit, fixed-precision context rather
than the caller's ambient one (Remediation Plan v1.2's Decimal-context hardening,
each module conventionally owns its own copy of this constant rather than
importing a private name across modules — matches `inventory_costing.py`/
`order_pricing.py`/`operational_recalculation_service.py`)."""


def _overflow_error(field: str) -> ApiError:
    return ApiError(
        422,
        _OVERFLOW_CODE,
        "A derived production value could not be represented for storage.",
        issues=[
            {
                "severity": "ERROR",
                "code": _OVERFLOW_CODE,
                "message": f"{field} is too large to store.",
                "field": field,
                "resource": "production_run",
                "details": {},
            }
        ],
    )


def _persist_quantity(value: Decimal, *, field: str) -> Decimal:
    quantized = quantize_for_storage(value)
    if not is_representable_in_numeric_18_6(quantized):
        raise _overflow_error(field)
    return quantized


def _persist_money(value: Decimal, *, field: str) -> Decimal:
    quantized = quantize_money(value)
    if not is_representable_in_numeric_14_2(quantized):
        raise _overflow_error(field)
    return quantized


def _persist_int32(value: int, *, field: str) -> int:
    if not is_representable_in_int32(value):
        raise _overflow_error(field)
    return value


def _cost_basis_unknown_error(ingredient_names: list[str]) -> ApiError:
    return ApiError(
        409,
        _COST_BASIS_UNKNOWN_CODE,
        "One or more ingredients have no known cost basis and Start cannot proceed "
        "without fabricating a cost.",
        issues=[
            {
                "severity": "ERROR",
                "code": _COST_BASIS_UNKNOWN_CODE,
                "message": (
                    f"'{name}' has no weighted-average, replacement, or latest "
                    "purchase cost on record."
                ),
                "field": None,
                "resource": "ingredient",
                "details": {"ingredient_name": name},
            }
            for name in ingredient_names
        ],
    )


def _requirement_changed_error() -> ApiError:
    return ApiError(
        409,
        _REQUIREMENT_CHANGED_CODE,
        "This production requirement has changed since it was last viewed. Refresh and try again.",
    )


def _already_started_error() -> ApiError:
    return ApiError(
        409,
        _ALREADY_STARTED_CODE,
        "This production requirement is already covered by an active production run.",
    )


def _resolve_cost_basis(ingredient: Ingredient) -> tuple[Decimal | None, CostBasisSource | None]:
    """The Start-time selected basis (Phase 8 Final Architecture Lock / Addendum §B):
    `physical_quantity > 0` -> WAC (even a legitimate 0); else -> the effective
    replacement/latest fallback. Reuses `resolve_planned_ingredient_unit_cost`'s exact
    value logic (never re-derived), and re-checks the same cheap `physical_quantity >
    0` condition only to label which branch produced that value — this is the
    discriminator persisted at Start and never re-decided at Finish."""
    selected = production_costing.resolve_planned_ingredient_unit_cost(
        physical_quantity=ingredient.physical_quantity,
        weighted_average_unit_cost=ingredient.weighted_average_unit_cost,
        replacement_unit_cost=ingredient.replacement_unit_cost,
        latest_purchase_unit_cost=ingredient.latest_purchase_unit_cost,
    )
    if selected is None:
        return None, None
    source = CostBasisSource.WAC if ingredient.physical_quantity > 0 else CostBasisSource.FALLBACK
    return selected, source


def _underproduction_fingerprint(
    *,
    requirement_id: uuid.UUID,
    production_demand_quantity: Decimal,
    batches: int,
    expected_output: Decimal,
) -> str:
    """A deterministic content-hash over exactly the authoritative, freshly-recomputed
    state the underproduction decision depends on (RUN-004; Phase 8 Final Architecture
    Lock §5, reusing the ADR-128 warning-fingerprint pattern) — never a client-supplied
    value, always recomputed fresh under the Start transaction's locks on every
    attempt."""
    payload = f"{requirement_id}:{production_demand_quantity}:{batches}:{expected_output}"
    return hashlib.sha256(payload.encode()).hexdigest()


def start_production(
    db: Session,
    business: Business,
    production_requirement_id: uuid.UUID,
    *,
    batches: int | None,
    acknowledged_warning_fingerprint: str | None,
) -> ProductionRun:
    """Start Production (RUN-003/005/006; Phase 8 Final Architecture Lock §5, §11).

    Locks, in order: Product (first — matching every other Product-touching
    workflow's relative order and serializing Start against any concurrent
    recalculation for this Product) -> the specific ProductionRequirement row
    (re-fetched under the Product lock; its existence is re-verified, never assumed
    from an earlier unlocked read) -> every distinct Ingredient referenced by the
    pinned RecipeRevision, sorted (so the run-start cost snapshot is atomic against a
    concurrent restock/adjustment). The RecipeRevision itself is read-only (immutable,
    no lock needed) and used exactly as pinned — never compared against the Product's
    current revision.
    """
    candidate = get_owned_or_404(db, ProductionRequirement, production_requirement_id, business)
    product = product_service.get_product_for_business_locked(db, candidate.product_id, business)

    requirement = db.scalar(
        select(ProductionRequirement)
        .where(
            ProductionRequirement.id == production_requirement_id,
            ProductionRequirement.business_id == business.id,
        )
        .with_for_update()
    )
    if requirement is None:
        raise _requirement_changed_error()

    try:
        if requirement.calculation_status is not CalculationStatus.CALCULATED:
            raise _requirement_changed_error()
        if requirement.recipe_revision_id is None:
            raise _requirement_changed_error()

        already_active = db.scalar(
            select(ProductionRun.id).where(
                ProductionRun.source_production_requirement_id == requirement.id,
                ProductionRun.business_id == business.id,
                ProductionRun.status == ProductionRunStatus.IN_PRODUCTION,
            )
        )
        if already_active is not None:
            raise _already_started_error()

        revision = db.scalar(
            select(RecipeRevision).where(
                RecipeRevision.id == requirement.recipe_revision_id,
                RecipeRevision.business_id == business.id,
            )
        )
        if revision is None:
            raise _requirement_changed_error()

        revision_ingredients = db.scalars(
            select(RecipeRevisionIngredient).where(
                RecipeRevisionIngredient.recipe_revision_id == revision.id,
                RecipeRevisionIngredient.business_id == business.id,
            )
        ).all()
        ingredient_ids = {line.ingredient_id for line in revision_ingredients}
        ingredients_by_id = ingredient_service.lock_ingredients_for_business(
            db, ingredient_ids, business
        )
        missing_names: list[str] = []
        for ingredient_id in sorted(ingredient_ids):
            ingredient = ingredients_by_id.get(ingredient_id)
            if ingredient is None:
                raise _requirement_changed_error()
            selected, _source = _resolve_cost_basis(ingredient)
            if selected is None:
                missing_names.append(ingredient.name)
        if missing_names:
            raise _cost_basis_unknown_error(sorted(missing_names))

        # RUN-004: authoritatively recompute the batch plan under lock — never trust
        # a client-cached recommendation.
        plan = recipe_scaling.calculate_whole_batch_plan(
            requirement.production_demand_quantity, revision.yield_quantity
        )
        chosen_batches = _persist_int32(
            batches if batches is not None else plan.required_batches, field="batches"
        )
        if chosen_batches <= 0:
            raise ApiError(
                422,
                "PRODUCTION_BATCHES_MUST_BE_POSITIVE",
                "At least one batch is required to start production.",
            )
        expected_output = recipe_scaling.scale_ingredient_quantity(
            revision.yield_quantity, chosen_batches
        )

        if expected_output < requirement.production_demand_quantity:
            fingerprint = _underproduction_fingerprint(
                requirement_id=requirement.id,
                production_demand_quantity=requirement.production_demand_quantity,
                batches=chosen_batches,
                expected_output=expected_output,
            )
            if acknowledged_warning_fingerprint != fingerprint:
                raise ApiError(
                    409,
                    _ACKNOWLEDGMENT_REQUIRED_CODE,
                    "The chosen batch count will not fully cover confirmed demand. "
                    "Review and acknowledge before starting.",
                    issues=[
                        {
                            "severity": "WARNING",
                            "code": "UNDERPRODUCTION",
                            "message": (
                                "Expected output "
                                f"({expected_output}) is less than remaining demand "
                                f"({requirement.production_demand_quantity})."
                            ),
                            "field": "batches",
                            "resource": "production_requirement",
                            "details": {
                                "expected_output": str(expected_output),
                                "production_demand_quantity": str(
                                    requirement.production_demand_quantity
                                ),
                                "fingerprint": fingerprint,
                            },
                        }
                    ],
                )

        domain_lines = [
            ingredient_requirements_domain.RecipeRevisionIngredientLine(
                ingredient_id=line.ingredient_id,
                quantity_per_batch=line.quantity,
                unit=line.unit,
                ingredient_canonical_unit=ingredients_by_id[line.ingredient_id].canonical_unit,
            )
            for line in revision_ingredients
        ]
        ingredient_reqs = [
            r
            for r in ingredient_requirements_domain.calculate_ingredient_requirements(
                domain_lines, chosen_batches
            )
            if r.required_quantity_canonical > 0
        ]

        labor_cost = production_costing.calculate_planned_labor_cost(
            batches=chosen_batches,
            active_minutes_per_batch=revision.active_time_minutes,
            labor_rate=business.default_labor_rate,
        )

        run_id = uuid.uuid4()
        run_ingredients: list[ProductionRunIngredient] = []
        estimated_ingredient_cost = Decimal(0)
        for req in ingredient_reqs:
            ingredient = ingredients_by_id[req.ingredient_id]
            selected, source = _resolve_cost_basis(ingredient)
            if selected is None or source is None:
                # Unreachable in practice: every ingredient's cost basis was already
                # validated as resolvable above, against the same locked row.
                raise _cost_basis_unknown_error([ingredient.name])
            replacement_snapshot = resolve_effective_replacement_cost(
                ingredient.replacement_unit_cost, ingredient.latest_purchase_unit_cost
            )
            with decimal.localcontext() as ctx:
                ctx.prec = _CONTEXT_PRECISION
                line_total = req.required_quantity_canonical * selected
                estimated_ingredient_cost += line_total
            run_ingredients.append(
                ProductionRunIngredient(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    production_run_id=run_id,
                    ingredient_id=req.ingredient_id,
                    planned_quantity=_persist_quantity(
                        req.required_quantity_canonical, field="planned_quantity"
                    ),
                    weighted_average_unit_cost_snapshot=_persist_quantity(
                        ingredient.weighted_average_unit_cost,
                        field="weighted_average_unit_cost_snapshot",
                    ),
                    replacement_unit_cost_snapshot=(
                        _persist_quantity(
                            replacement_snapshot, field="replacement_unit_cost_snapshot"
                        )
                        if replacement_snapshot is not None
                        else None
                    ),
                    cost_basis_source=source,
                )
            )

        estimated_total_production_cost = estimated_ingredient_cost + labor_cost

        run = ProductionRun(
            id=run_id,
            business_id=business.id,
            source_production_requirement_id=requirement.id,
            product_id=product.id,
            recipe_revision_id=revision.id,
            status=ProductionRunStatus.IN_PRODUCTION,
            planned_batches=chosen_batches,
            planned_output_quantity=_persist_quantity(
                expected_output, field="planned_output_quantity"
            ),
            recipe_yield_snapshot=_persist_quantity(
                revision.yield_quantity, field="recipe_yield_snapshot"
            ),
            active_minutes_per_batch_snapshot=_persist_int32(
                revision.active_time_minutes, field="active_minutes_per_batch_snapshot"
            ),
            elapsed_minutes_per_batch_snapshot=(
                _persist_int32(
                    revision.elapsed_time_minutes, field="elapsed_minutes_per_batch_snapshot"
                )
                if revision.elapsed_time_minutes is not None
                else None
            ),
            labor_rate_snapshot=_persist_money(
                business.default_labor_rate, field="labor_rate_snapshot"
            ),
            estimated_ingredient_cost=_persist_quantity(
                estimated_ingredient_cost, field="estimated_ingredient_cost"
            ),
            estimated_labor_cost=_persist_quantity(labor_cost, field="estimated_labor_cost"),
            estimated_total_production_cost=_persist_quantity(
                estimated_total_production_cost, field="estimated_total_production_cost"
            ),
            started_at=datetime.now(UTC),
        )
        db.add(run)
        for run_ingredient in run_ingredients:
            db.add(run_ingredient)
        db.flush()
    except ApiError:
        db.rollback()
        raise

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "ix_production_runs_unique_active_source" in str(exc.orig):
            raise _already_started_error() from None
        raise

    return run


def _not_in_production_error(action: str) -> ApiError:
    return ApiError(
        409,
        "PRODUCTION_RUN_NOT_IN_PRODUCTION",
        f"Only a currently active production run can be {action}.",
    )


_MAX_DISCOVERY_RETRIES = 3


class _CandidateOrderSetGrew(Exception):
    """Internal-only retry signal (Phase 8 Final Architecture Lock §11's discover-
    lock-verify-retry protocol) — never surfaced to a caller. Raised when a new
    CONFIRMED/READY Order for this Product appeared between the non-locking
    discovery read and the Product lock; caught by the owning workflow's outer
    bounded-retry loop, which rolls back everything (including the ProductionRun
    lock) and restarts from a fully unlocked state, exactly as the protocol
    requires — never acquiring a new Order lock after Product is already held."""


def cancel_production_run(
    db: Session,
    business: Business,
    production_run_id: uuid.UUID,
    *,
    expected_version: int,
    business_today: date,
) -> tuple[ProductionRun, RecalcResult]:
    """Bounded-retry wrapper around `_cancel_production_run_attempt` — matches
    `finish_production`'s identical shape (Independent Implementation Review
    Remediation Plan v1.2 §1)."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _cancel_production_run_attempt(
                db,
                business,
                production_run_id,
                expected_version=expected_version,
                business_today=business_today,
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "PRODUCTION_CANCEL_CONTENTION",
        "Too many concurrent changes to this product's demand; please retry.",
    )


def _cancel_production_run_attempt(
    db: Session,
    business: Business,
    production_run_id: uuid.UUID,
    *,
    expected_version: int,
    business_today: date,
) -> tuple[ProductionRun, RecalcResult]:
    """Cancel (RUN-002; Phase 8 Final Architecture Lock §4/§11; corrected by
    Independent Implementation Review Remediation Plan v1.2 §1). No Ingredient
    inventory was ever consumed by Start, so no physical reversal occurs — this is a
    pure status transition plus a recalculation rebuild of the now-freed demand.

    **May change another Order's readiness** (Remediation Plan v1.2 §1, withdrawing
    the earlier "no readiness impact" classification): while `run` is
    `IN_PRODUCTION`, its source `ProductionRequirement`'s Surplus allocations are
    excluded from ordinary FEFO competition (`fixed_lot_allocations`, gated by
    `get_protected_requirement_ids`) — other demand for the same Product must
    allocate around that fixed claim. The instant this cancellation flips the run's
    status, that specific protection disappears, and the recalculation this same
    transaction triggers sees a strictly larger rebuildable pool than existed a
    moment before, with physical supply and total demand otherwise unchanged — a
    higher-FEFO-priority (earlier-deadline) CONFIRMED Order that previously
    couldn't touch that fixed claim can now legitimately win it.

    Locks, in order (unchanged position of `ProductionRun` relative to `Order` —
    see the module's own composed hierarchy; the original Final Architecture Lock
    §11 hierarchy is preserved verbatim, this only newly populates the `Order`
    position, which Cancel previously skipped entirely): `ProductionRun` (self,
    single disjoint-per-call row — never contested combinatorially the way Orders
    are, so locking it first here creates no cross-transaction ordering conflict
    with any Order-only or Product-only workflow) -> the complete candidate `Order`
    set, discovered non-locking and acquired sorted in one phase (mirroring
    `_finish_production_attempt`'s identical, already-proven step, inserted here in
    the same position Cancel previously omitted) -> `Product` -> re-verify the
    candidate set under Product serialization, retry the whole attempt if grown ->
    `Recipe`/`Ingredient` (union)/`Surplus`/`Purchased` inventory (the standard
    recalculation caller-contract)."""
    run = db.scalar(
        select(ProductionRun)
        .where(ProductionRun.id == production_run_id, ProductionRun.business_id == business.id)
        .with_for_update()
    )
    if run is None:
        raise NotFoundError()

    try:
        check_version(run, expected_version, resource="production_run")
        if run.status is not ProductionRunStatus.IN_PRODUCTION:
            raise _not_in_production_error("canceled")

        # --- discover-lock-verify-retry: Order(s), sorted, before Product ----------
        # Identical shape to `_finish_production_attempt` below — reused, not
        # reinvented (Remediation Plan v1.2 §1).
        candidate_ids_s0 = readiness_service.discover_candidate_order_ids(
            db, business, run.product_id
        )
        locked_orders: dict[uuid.UUID, Order] = {}
        if candidate_ids_s0:
            locked_orders = {
                o.id: o
                for o in db.scalars(
                    select(Order)
                    .where(Order.id.in_(candidate_ids_s0), Order.business_id == business.id)
                    .order_by(Order.id)
                    .with_for_update()
                )
            }

        product = product_service.get_product_for_business_locked(db, run.product_id, business)

        candidate_ids_s1 = readiness_service.discover_candidate_order_ids(
            db, business, run.product_id
        )
        if not set(candidate_ids_s1).issubset(locked_orders.keys()):
            raise _CandidateOrderSetGrew()

        order_lifecycle_service._acquire_recipe_and_ingredient_locks(db, business, [product])
        order_lifecycle_service._acquire_surplus_and_purchased_locks(db, business, [product])

        run.status = ProductionRunStatus.CANCELED
        run.canceled_at = datetime.now(UTC)
        db.flush()

        # The run's own status is already CANCELED (flushed), so
        # `get_protected_requirement_ids` naturally no longer treats its source
        # requirement as protected — this recalculation pass rebuilds that now-freed
        # demand fresh, same composition pattern as Order cancel. `PRESERVE_READY`
        # (the default) is correct here: this workflow's readiness impact comes
        # entirely from the orthogonal `IN_PRODUCTION`-protection axis being
        # removed, never from any READY-Surplus targeting — every OTHER lot's
        # READY-linked allocations remain exactly as fixed as any ordinary
        # recalculation would leave them.
        result = recalculate_product_closure(db, business, product, business_today=business_today)
        for order in locked_orders.values():
            readiness_service.evaluate_and_apply_readiness(db, business, order)
    except ApiError:
        db.rollback()
        raise

    commit_or_raise_stale(db, resource="production_run")
    return run, result


# --- Finish Production ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FinishProductionResult:
    run: ProductionRun
    recalc_result: RecalcResult
    negative_inventory_ingredient_ids: list[uuid.UUID]
    is_shortfall: bool


def _completed_and_surplus_totals_for_line(
    db: Session, business: Business, order_line_id: uuid.UUID
) -> tuple[Decimal, Decimal]:
    completed = db.scalar(
        select(func.coalesce(func.sum(ProductionRunOrderAllocation.quantity), 0)).where(
            ProductionRunOrderAllocation.order_line_id == order_line_id,
            ProductionRunOrderAllocation.business_id == business.id,
        )
    )
    active_surplus = db.scalar(
        select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
            SurplusAllocation.order_line_id == order_line_id,
            SurplusAllocation.business_id == business.id,
        )
    )
    return completed, active_surplus


def finish_production(
    db: Session,
    business: Business,
    production_run_id: uuid.UUID,
    *,
    expected_version: int,
    actual_output_quantity: Decimal,
    ingredient_actual_quantities: dict[uuid.UUID, Decimal] | None,
    business_today: date,
) -> FinishProductionResult:
    """Finish Production (RUN-007..012; Phase 8 Final Architecture Amendment §1) —
    the single highest-risk Phase 8 write. One atomic transaction, no partial
    commits. Ingredient consumption is unconditional on output (a real production
    event occurred even when nothing usable resulted); output allocation, cost
    apportionment, and Surplus creation only happen when `actual_output_quantity >
    0`. `recalculate_product_closure` and whole-Order READY reevaluation always run,
    in both branches (RUN-012: unmet demand must remain/return as active demand)."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _finish_production_attempt(
                db,
                business,
                production_run_id,
                expected_version=expected_version,
                actual_output_quantity=actual_output_quantity,
                ingredient_actual_quantities=ingredient_actual_quantities,
                business_today=business_today,
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "PRODUCTION_FINISH_CONTENTION",
        "Too many concurrent changes to this product's demand; please retry.",
    )


def _finish_production_attempt(
    db: Session,
    business: Business,
    production_run_id: uuid.UUID,
    *,
    expected_version: int,
    actual_output_quantity: Decimal,
    ingredient_actual_quantities: dict[uuid.UUID, Decimal] | None,
    business_today: date,
) -> FinishProductionResult:
    run = db.scalar(
        select(ProductionRun)
        .where(ProductionRun.id == production_run_id, ProductionRun.business_id == business.id)
        .with_for_update()
    )
    if run is None:
        raise NotFoundError()

    try:
        check_version(run, expected_version, resource="production_run")
        if run.status is not ProductionRunStatus.IN_PRODUCTION:
            raise _not_in_production_error("finished")
        if actual_output_quantity < 0:
            raise ApiError(
                422,
                "PRODUCTION_ACTUAL_OUTPUT_INVALID",
                "Actual output quantity cannot be negative.",
            )
        # Quantize once, immediately, at this persistence-world boundary — every
        # subsequent read (the positive/zero-output branch condition, persistence,
        # every downstream allocation/apportionment call) uses this single
        # already-quantized value, never the raw input (Remediation Plan v1.2 §7 /
        # Finding 10): a sub-quantum positive input like `Decimal("0.0000004")`
        # quantizes to `Decimal("0.000000")` here and correctly takes the
        # already-approved zero-output path below, rather than reaching
        # `apportion_production_cost` with an effectively-zero quantity and raising
        # an uncaught `ValueError`.
        actual_output_quantity = _persist_quantity(
            actual_output_quantity, field="actual_output_quantity"
        )

        # --- discover-lock-verify-retry: Order(s), sorted, before Product ----------
        candidate_ids_s0 = readiness_service.discover_candidate_order_ids(
            db, business, run.product_id
        )
        locked_orders: dict[uuid.UUID, Order] = {}
        if candidate_ids_s0:
            locked_orders = {
                o.id: o
                for o in db.scalars(
                    select(Order)
                    .where(Order.id.in_(candidate_ids_s0), Order.business_id == business.id)
                    .order_by(Order.id)
                    .with_for_update()
                )
            }

        product = product_service.get_product_for_business_locked(db, run.product_id, business)

        candidate_ids_s1 = readiness_service.discover_candidate_order_ids(
            db, business, run.product_id
        )
        if not set(candidate_ids_s1).issubset(locked_orders.keys()):
            raise _CandidateOrderSetGrew()

        order_lifecycle_service._acquire_recipe_and_ingredient_locks(db, business, [product])
        order_lifecycle_service._acquire_surplus_and_purchased_locks(db, business, [product])

        # --- Ingredient consumption — unconditional on output (steps 2-6) --------
        run_ingredients = list(
            db.scalars(
                select(ProductionRunIngredient).where(
                    ProductionRunIngredient.production_run_id == run.id,
                    ProductionRunIngredient.business_id == business.id,
                )
            )
        )
        run_ingredient_ids = {ri.ingredient_id for ri in run_ingredients}
        if ingredient_actual_quantities:
            unknown_ids = set(ingredient_actual_quantities) - run_ingredient_ids
            if unknown_ids:
                raise ApiError(
                    422,
                    "PRODUCTION_FINISH_UNKNOWN_INGREDIENT_OVERRIDE",
                    "One or more ingredient overrides do not belong to this run.",
                    issues=[
                        {
                            "severity": "ERROR",
                            "code": "PRODUCTION_FINISH_UNKNOWN_INGREDIENT_OVERRIDE",
                            "message": "This ingredient is not part of this production run.",
                            "field": "ingredient_actual_quantities",
                            "resource": "ingredient",
                            "details": {"ingredient_id": str(ingredient_id)},
                        }
                        for ingredient_id in sorted(unknown_ids)
                    ],
                )
        ingredients_by_id = ingredient_service.lock_ingredients_for_business(
            db, run_ingredient_ids, business
        )

        negative_inventory_ingredient_ids: list[uuid.UUID] = []
        actual_ingredient_cost = Decimal(0)
        for ri in run_ingredients:
            override = (
                ingredient_actual_quantities.get(ri.ingredient_id)
                if ingredient_actual_quantities
                else None
            )
            raw_actual_qty = override if override is not None else ri.planned_quantity
            if raw_actual_qty < 0:
                raise ApiError(
                    422,
                    "PRODUCTION_INGREDIENT_USAGE_INVALID",
                    "Actual ingredient usage cannot be negative.",
                )
            actual_qty = _persist_quantity(raw_actual_qty, field="actual_quantity")

            unit_cost = (
                ri.weighted_average_unit_cost_snapshot
                if ri.cost_basis_source is CostBasisSource.WAC
                else ri.replacement_unit_cost_snapshot
            )
            with decimal.localcontext() as ctx:
                ctx.prec = _CONTEXT_PRECISION
                actual_total = actual_qty * unit_cost
                actual_ingredient_cost += actual_total

            ri.actual_quantity = actual_qty
            ri.actual_unit_cost_basis = _persist_quantity(unit_cost, field="actual_unit_cost_basis")
            ri.actual_total_cost = _persist_quantity(actual_total, field="actual_total_cost")

            ingredient = ingredients_by_id[ri.ingredient_id]
            new_physical_quantity = _persist_quantity(
                ingredient.physical_quantity - actual_qty, field="physical_quantity"
            )
            if new_physical_quantity < 0:
                negative_inventory_ingredient_ids.append(ingredient.id)
            ingredient.physical_quantity = new_physical_quantity

            db.add(
                InventoryTransaction(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    ingredient_id=ingredient.id,
                    transaction_type=InventoryTransactionType.PRODUCTION_CONSUMPTION,
                    quantity_change=-actual_qty,
                    unit_cost=ri.actual_unit_cost_basis,
                    total_cost=ri.actual_total_cost,
                    production_run_id=run.id,
                )
            )

        actual_labor_cost = run.estimated_labor_cost  # snapshot values never change at Finish
        actual_total_production_cost = _persist_quantity(
            actual_ingredient_cost + actual_labor_cost, field="actual_total_production_cost"
        )

        # Already quantized at the persistence-world boundary above — assigned
        # directly, never re-quantized from an already-quantized intermediate.
        run.actual_output_quantity = actual_output_quantity
        run.actual_ingredient_cost = _persist_quantity(
            actual_ingredient_cost, field="actual_ingredient_cost"
        )
        run.actual_labor_cost = actual_labor_cost
        run.actual_total_production_cost = actual_total_production_cost
        run.completed_at = datetime.now(UTC)
        run.status = ProductionRunStatus.COMPLETED
        db.flush()

        # --- Branch on output (step 8) --------------------------------------------
        if actual_output_quantity > 0:
            demand_lines: list[OutstandingDemandLine] = []
            for order in locked_orders.values():
                for line in order.lines:
                    if line.product_id != product.id or order.fulfillment_date is None:
                        continue
                    completed, active_surplus = _completed_and_surplus_totals_for_line(
                        db, business, line.id
                    )
                    demand_lines.append(
                        OutstandingDemandLine(
                            order_id=order.id,
                            order_line_id=line.id,
                            product_id=product.id,
                            underlying_quantity=line.underlying_quantity,
                            completed_allocation_quantity=completed,
                            active_surplus_allocation_quantity=active_surplus,
                            fulfillment_date=order.fulfillment_date,
                            fulfillment_time=order.fulfillment_time,
                        )
                    )

            outcome = allocate_production_output(
                demand_lines, run.actual_output_quantity, product_id=product.id
            )

            order_id_by_line_id = {a.order_line_id: a.order_id for a in outcome.allocations}
            allocation_rows: dict[uuid.UUID, ProductionRunOrderAllocation] = {}
            for alloc in outcome.allocations:
                row = ProductionRunOrderAllocation(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    production_run_id=run.id,
                    order_id=alloc.order_id,
                    order_line_id=alloc.order_line_id,
                    quantity=_persist_quantity(alloc.quantity, field="allocation_quantity"),
                )
                db.add(row)
                allocation_rows[alloc.order_line_id] = row
            db.flush()

            apportionment = apportion_production_cost(
                actual_total_production_cost=run.actual_total_production_cost,
                actual_output_quantity=run.actual_output_quantity,
                direct_recipients=[
                    CostRecipient(order_line_id=a.order_line_id, quantity=a.quantity)
                    for a in outcome.allocations
                ],
                surplus_excess_quantity=outcome.remaining_excess,
            )
            for order_line_id, amount in apportionment.order_line_amounts.items():
                db.add(
                    OrderCostAllocation(
                        id=uuid.uuid4(),
                        business_id=business.id,
                        order_id=order_id_by_line_id[order_line_id],
                        order_line_id=order_line_id,
                        cost_type=CostType.PRODUCTION,
                        amount=amount,
                        production_run_order_allocation_id=allocation_rows[order_line_id].id,
                    )
                )

            if outcome.remaining_excess > 0:
                business_local_date = run.completed_at.astimezone(
                    ZoneInfo(business.timezone)
                ).date()
                usable_through_date = (
                    business_local_date + timedelta(days=product.default_surplus_usable_days)
                    if product.default_surplus_usable_days is not None
                    else None
                )
                with decimal.localcontext() as ctx:
                    ctx.prec = _CONTEXT_PRECISION
                    cost_per_unit = run.actual_total_production_cost / run.actual_output_quantity
                surplus_row = SurplusInventory(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    product_id=product.id,
                    source_production_run_id=run.id,
                    physical_quantity=_persist_quantity(
                        outcome.remaining_excess, field="physical_quantity"
                    ),
                    unit_cost_basis=_persist_quantity(cost_per_unit, field="unit_cost_basis"),
                    source_total_cost_basis=apportionment.surplus_source_total_cost_basis,
                    produced_at=run.completed_at,
                    usable_through_date=usable_through_date,
                    is_reusable=product.can_reuse_surplus,
                )
                db.add(surplus_row)
                db.flush()
                db.add(
                    SurplusTransaction(
                        id=uuid.uuid4(),
                        business_id=business.id,
                        surplus_inventory_id=surplus_row.id,
                        transaction_type=SurplusTransactionType.PRODUCTION_OUTPUT,
                        quantity_change=outcome.remaining_excess,
                    )
                )
            db.flush()

        # --- Always: recalculate + whole-Order READY reevaluation (steps 9-10) ---
        recalc_result = recalculate_product_closure(
            db, business, product, business_today=business_today
        )
        for order in locked_orders.values():
            readiness_service.evaluate_and_apply_readiness(db, business, order)
    except ApiError:
        db.rollback()
        raise

    commit_or_raise_stale(db, resource="production_run")
    is_shortfall = actual_output_quantity == 0 or any(
        req.production_demand_quantity > 0 for req in recalc_result.requirements
    )
    return FinishProductionResult(
        run=run,
        recalc_result=recalc_result,
        negative_inventory_ingredient_ids=negative_inventory_ingredient_ids,
        is_shortfall=is_shortfall,
    )


# --- Production Plan / Review listing ----------------------------------------------


def list_production_requirements_for_business(
    db: Session, business: Business, *, limit: int = 50, offset: int = 0
) -> tuple[list[ProductionRequirement], int]:
    """Every currently-actionable `ProductionRequirement` — `CALCULATED` (has a
    recipe), genuinely needing production (`production_demand_quantity > 0`), and
    not already covered by an active `IN_PRODUCTION` run — powers the Production
    Plan/Review screen's candidate list. A protected (already-active) requirement is
    excluded here; its own `ProductionRun` already appears in Active Runs."""
    protected_subquery = (
        select(ProductionRun.source_production_requirement_id)
        .where(
            ProductionRun.business_id == business.id,
            ProductionRun.status == ProductionRunStatus.IN_PRODUCTION,
            ProductionRun.source_production_requirement_id.is_not(None),
        )
        .scalar_subquery()
    )
    conditions = (
        ProductionRequirement.business_id == business.id,
        ProductionRequirement.calculation_status == CalculationStatus.CALCULATED,
        ProductionRequirement.production_demand_quantity > 0,
        ProductionRequirement.id.not_in(protected_subquery),
    )
    total = (
        db.scalar(select(func.count()).select_from(ProductionRequirement).where(*conditions)) or 0
    )
    items = list(
        db.scalars(
            select(ProductionRequirement)
            .where(*conditions)
            .order_by(ProductionRequirement.demand_date.asc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total
