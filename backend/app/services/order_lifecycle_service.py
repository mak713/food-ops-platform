"""Order confirmation and cancellation (Spec §4.2; Phase 7 Plan v2 §6/§11, corrected
by the Final Pre-Implementation Amendment and Final Architecture Lock §A/§B/§C/§F).

Each public function here is the single transaction owner for its workflow: it locks,
validates, recalculates (via `operational_recalculation_service`, non-committing),
`flush()`-es so the recalculation sees its own pending mutation, and commits exactly
once — never a commit followed by a separate recalculation call (Final Architecture
Lock §A).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.tenant import (
    NotFoundError,
    check_version,
    commit_or_raise_stale,
    lock_recipe_for_product,
)
from app.db.enums import CostType, OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.product import Product, SellingOption
from app.db.models.production import ProductionRunOrderAllocation
from app.db.models.purchased_inventory import PurchasedProductInventory
from app.db.models.surplus import SurplusInventory
from app.domain import order_pricing
from app.services import ingredient_service, order_service, readiness_service
from app.services.operational_recalculation_service import (
    IngredientShortageResult,
    RecalcResult,
    compute_combined_ingredient_shortages,
    get_production_locked_order_line_ids,
    recalculate_product_closure,
    revisions_participating_in_closure,
    validate_fulfillment_local_time,
)

_MAX_DISCOVERY_RETRIES = 3
"""Matches the identical bounded-retry constant already used by
`production_run_service.py`/`surplus_service.py`/`purchased_inventory_service.py`
for the same discover-lock-verify-retry protocol, reused here (Independent
Implementation Review Remediation Plan v1.1 §1/v1.2)."""


class _CandidateOrderSetGrew(Exception):
    """Internal-only retry signal — mirrors the identical exception already defined
    in `production_run_service.py`/`surplus_service.py`; a distinct instance per
    module by established convention (never a shared cross-module exception type)."""


def _discover_full_order_set(
    db: Session, business: Business, order_id: uuid.UUID, product_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """Non-locking discovery of every Order this recalculation could need to lock —
    the directly-addressed Order (`order_id`, included unconditionally, even while
    still `DRAFT` and therefore invisible to `discover_candidate_order_ids`' own
    CONFIRMED/READY filter) union every other CONFIRMED/READY Order referencing any
    of `product_ids` (Remediation Plan v1.2 §1 — every Order lock, including the
    directly-addressed one, is acquired in exactly one globally-sorted phase; never
    lock the directly-addressed Order separately/first)."""
    ids = {order_id}
    for product_id in product_ids:
        ids |= set(readiness_service.discover_candidate_order_ids(db, business, product_id))
    return ids


def _lock_orders_sorted(
    db: Session, business: Business, order_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Order]:
    return {
        o.id: o
        for o in db.scalars(
            select(Order)
            .where(Order.id.in_(order_ids), Order.business_id == business.id)
            .order_by(Order.id)
            .with_for_update()
        )
    }


def _preflight_resolve_payload_product_ids(
    db: Session, business: Business, payload
) -> set[uuid.UUID]:
    """Non-locking, best-effort resolution of a proposed edit payload's Product
    references — discovery-only (Remediation Plan v1.2 §1, correcting v1.1 §2's
    `update_confirmed_order` product-universe gap: a Product a line is being
    CHANGED TO, or a brand-new Product-backed line, must be discovered against too,
    not only the currently-persisted Products). A garbage/foreign-tenant/nonexistent
    `selling_option_id` here simply fails to contribute a product id — harmless,
    since the authoritative resolution/validation remains
    `order_service._prepare_order_edit`, unchanged, run later under lock."""
    product_ids: set[uuid.UUID] = set()
    selling_option_ids: set[uuid.UUID] = set()
    for line in payload.lines:
        if line.product_id is not None:
            product_ids.add(line.product_id)
        elif line.selling_option_id is not None:
            selling_option_ids.add(line.selling_option_id)
    if selling_option_ids:
        product_ids |= set(
            db.scalars(
                select(SellingOption.product_id).where(
                    SellingOption.id.in_(selling_option_ids),
                    SellingOption.business_id == business.id,
                )
            )
        )
    return product_ids


def get_order_production_locked_line_ids(
    db: Session, business: Business, order: Order
) -> set[uuid.UUID]:
    """Every OrderLine on this Order currently covered by an active `IN_PRODUCTION`
    run, across all of its referenced Products. Read-only — never re-acquires a
    lock; used both by the write-time precondition below and by the advisory
    `OrderResponse.production_locked` read model."""
    product_ids = {line.product_id for line in order.lines if line.product_id is not None}
    locked: set[uuid.UUID] = set()
    for product_id in product_ids:
        product = db.scalar(
            select(Product).where(Product.id == product_id, Product.business_id == business.id)
        )
        if product is None:
            continue
        locked |= get_production_locked_order_line_ids(db, business, product)
    return {line.id for line in order.lines if line.id in locked}


def _production_locked_error(locked_ids: set[uuid.UUID]) -> ApiError:
    return ApiError(
        409,
        "ORDER_PRODUCTION_LOCKED",
        "This order has demand currently covered by an active production run "
        "and cannot be changed until that run completes or is canceled.",
        issues=[
            {
                "severity": "ERROR",
                "code": "ORDER_PRODUCTION_LOCKED",
                "message": "This line's demand is covered by an active production run.",
                "field": "lines",
                "resource": "order_line",
                "details": {"order_line_id": str(line_id)},
            }
            for line_id in sorted(locked_ids)
        ],
    )


def _reject_if_production_locked(db: Session, business: Business, order: Order) -> None:
    """The active-Production-Run precondition (Plan v2 §6), whole-Order form: rejects
    the whole operation if ANY of this Order's demand is currently production-locked
    — correct for Cancel, which is all-or-nothing by nature (Final Architecture Lock
    §C). A demand-affecting confirmed EDIT uses the finer-grained, per-line form
    instead — see `_reject_if_confirmed_edit_touches_protected_demand` below."""
    locked_ids = get_order_production_locked_line_ids(db, business, order)
    if locked_ids:
        raise _production_locked_error(locked_ids)


def _line_is_demand_affecting_change(line, existing) -> bool:
    """A line-level edit is demand-affecting (Plan v2 §11 table) when it changes
    Product/source identity or quantity — never price/notes-only. A brand-new line
    (`existing is None`) is always demand-affecting (new demand).

    Line-type aware (Phase 7 Final Remediation Correction Plan, Finding 2):
    STANDARD_OPTION's client-submitted `underlying_quantity` is always `None` (it is
    server-derived from `package_quantity` × the Selling Option's `quantity_units` —
    `OrderLineInput._validate_shape` never requires or reads it for this line type),
    while the persisted `OrderLine.underlying_quantity` is a real, `NOT NULL`, `> 0`
    column — comparing them directly (as a type-blind check would) is always
    "changed," misclassifying every STANDARD_OPTION edit, including a pure
    price-only one, as demand-affecting. The true STANDARD_OPTION quantity signal is
    `package_quantity` (matching `order_service._apply_standard_option_line`'s own
    `quantity_changed` rule); Selling-Option identity is a second demand-affecting
    dimension distinct from Product identity for this line type. CUSTOM_QUANTITY's
    `underlying_quantity` genuinely is client-submitted and comparable directly.
    CUSTOM_ITEM never creates Produced/Purchased demand at all (ORD-009/010) — it is
    never demand-affecting here; whether its *manual workload* guidance needs
    refreshing is a distinct concern Preview computes fresh from live confirmed-world
    state (Finding 6), not something this Order-mutation path triggers."""
    if existing is None:
        return True
    if existing.product_id != line.product_id:
        return True
    if line.line_type == OrderLineType.STANDARD_OPTION:
        if existing.selling_option_id != line.selling_option_id:
            return True
        return existing.package_quantity != line.package_quantity
    if line.line_type == OrderLineType.CUSTOM_QUANTITY:
        return existing.underlying_quantity != line.underlying_quantity
    return False  # CUSTOM_ITEM: never Produced/Purchased demand-affecting


def _is_demand_affecting_edit(order: Order, payload) -> bool:
    """Whole-edit demand-affecting test (Plan v2 §11 table; Final Architecture Lock
    §F): fulfillment_date/fulfillment_time are Order-header fields shared by every
    line's demand_date/timing, so EITHER changing is demand-affecting regardless of
    line content. Otherwise: any line add, remove, Product/source change, or
    quantity change. Pricing/notes/customer-financial-only fields never trigger
    this — matching the frozen ADR-118 per-dimension checks `_reconcile_line`
    already applies, reused here only to decide whether recalculation is even
    needed, never to reimplement them."""
    if payload.fulfillment_date != order.fulfillment_date:
        return True
    if payload.fulfillment_time != order.fulfillment_time:
        return True

    existing_lines_by_id = {line.id: line for line in order.lines}
    submitted_ids = {line.id for line in payload.lines if line.id is not None}
    if set(existing_lines_by_id) - submitted_ids:
        return True  # at least one existing line was removed

    return any(
        _line_is_demand_affecting_change(
            line, existing_lines_by_id.get(line.id) if line.id is not None else None
        )
        for line in payload.lines
    )


def _reject_if_confirmed_edit_touches_protected_demand(
    db: Session, business: Business, order: Order, payload
) -> None:
    """The active-Production-Run precondition (Plan v2 §6), per-line form for a
    confirmed EDIT (Final Architecture Lock §C — "blocks only the specific backend
    mutation that would alter protected operational demand... per-line/per-
    ProductionRequirement, not per-Order"): an edit to an unrelated, unprotected
    line on the same Order remains fully permitted. Must run against the Order's
    CURRENT (pre-mutation) line state — called before `_apply_order_edit`."""
    protected_ids = get_order_production_locked_line_ids(db, business, order)
    if not protected_ids:
        return

    # fulfillment_date/time are Order-header fields shared by every line's
    # demand_date/timing — changing either would rewrite every protected line's
    # demand uniformly, so ANY protected line on the Order blocks a header change.
    header_changed = (
        payload.fulfillment_date != order.fulfillment_date
        or payload.fulfillment_time != order.fulfillment_time
    )
    if header_changed:
        raise _production_locked_error(protected_ids)

    existing_lines_by_id = {line.id: line for line in order.lines}
    submitted_ids = {line.id for line in payload.lines if line.id is not None}
    touched: set[uuid.UUID] = {
        line_id for line_id in protected_ids if line_id not in submitted_ids
    }  # a protected line silently removed from the submission

    for line in payload.lines:
        if line.id in protected_ids and _line_is_demand_affecting_change(
            line, existing_lines_by_id.get(line.id)
        ):
            touched.add(line.id)

    if touched:
        raise _production_locked_error(touched)


def _affected_products(db: Session, business: Business, order: Order) -> list[Product]:
    product_ids = sorted({line.product_id for line in order.lines if line.product_id is not None})
    if not product_ids:
        return []
    return list(
        db.scalars(
            select(Product)
            .where(Product.id.in_(product_ids), Product.business_id == business.id)
            .order_by(Product.id)
            .with_for_update()
        )
    )


def _acquire_recipe_and_ingredient_locks(
    db: Session, business: Business, products: list[Product]
) -> None:
    """`Recipe` (per Product, in the same sorted Product order already established
    by `_affected_products`), then the union of every affected Ingredient across
    those Products' *current* Recipe Revision AND every retained historical pin
    still referenced by an existing link for that Product (Finding 6 of the Phase 7
    Implementation Remediation Plan — a retained line pinned to a non-current
    revision must have that revision's ingredients locked too, since the
    cross-Product shortage formula reads every active reservation for a shared
    Ingredient under its own lock, Final Architecture Lock §E), sorted (Plan v2 §7,
    corrected by Final Architecture Lock §B — Product is always locked before
    Recipe, Recipe before Ingredient, never reversed)."""
    from app.db.models.recipe import Recipe, RecipeRevision, RecipeRevisionIngredient

    ingredient_ids: set[uuid.UUID] = set()
    for product in products:
        recipe = db.scalar(
            select(Recipe).where(Recipe.product_id == product.id, Recipe.business_id == business.id)
        )
        if recipe is None:
            continue
        lock_recipe_for_product(db, product)
        current = db.scalar(
            select(RecipeRevision).where(
                RecipeRevision.recipe_id == recipe.id,
                RecipeRevision.business_id == business.id,
                RecipeRevision.is_current.is_(True),
            )
        )
        revision_ids = revisions_participating_in_closure(
            db, business, product, current_revision_id=current.id if current is not None else None
        )
        if not revision_ids:
            continue
        ingredient_ids |= set(
            db.scalars(
                select(RecipeRevisionIngredient.ingredient_id).where(
                    RecipeRevisionIngredient.recipe_revision_id.in_(revision_ids),
                    RecipeRevisionIngredient.business_id == business.id,
                )
            )
        )
    if ingredient_ids:
        ingredient_service.lock_ingredients_for_business(db, ingredient_ids, business)


def _acquire_surplus_and_purchased_locks(
    db: Session, business: Business, products: list[Product]
) -> None:
    for product in products:
        db.scalars(
            select(SurplusInventory)
            .where(
                SurplusInventory.product_id == product.id,
                SurplusInventory.business_id == business.id,
            )
            .order_by(SurplusInventory.id)
            .with_for_update()
        ).all()
        if product.product_type == ProductType.PURCHASED:
            db.scalar(
                select(PurchasedProductInventory)
                .where(
                    PurchasedProductInventory.product_id == product.id,
                    PurchasedProductInventory.business_id == business.id,
                )
                .with_for_update()
            )


def _acquire_operational_lock_graph(db: Session, business: Business, order: Order) -> list[Product]:
    """Composed order (Plan v2 §7, corrected by Final Pre-Implementation Amendment
    §5 and Final Architecture Lock §B): the Order row is already locked by the
    caller; Product (every operationally-affected Product, sorted) -> Recipe (per
    Product, same order) -> Ingredient (union, sorted) -> Surplus/Purchased
    inventory state (sorted)."""
    products = _affected_products(db, business, order)
    _acquire_recipe_and_ingredient_locks(db, business, products)
    _acquire_surplus_and_purchased_locks(db, business, products)
    return products


def _recalculate_all(
    db: Session, business: Business, products: list[Product], *, business_today
) -> list[RecalcResult]:
    return [
        recalculate_product_closure(db, business, product, business_today=business_today)
        for product in products
    ]


def _structural_readiness_error(issues: list[order_pricing.ConfirmationIssue]) -> ApiError:
    """The `ORDER_NOT_CONFIRMABLE` 422 wrapper shape for a
    `check_structural_confirmation_readiness`/`check_candidate_structural_readiness`
    issue list — shared by `confirm_order` (Draft->Confirmed) and
    `update_confirmed_order`/`order_preview_service.preview_order` (a CONFIRMED
    Order's own candidate-state guard, Phase 7 Final Lifecycle Invariant Correction
    Plan, Finding A/B) so the identical rejection shape is never independently
    duplicated at each call site."""
    return ApiError(
        422,
        "ORDER_NOT_CONFIRMABLE",
        "This order is not yet structurally ready for confirmation.",
        issues=[
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "field": i.field,
                "resource": "order",
                "details": {},
            }
            for i in issues
        ],
    )


def confirm_order(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    *,
    expected_version: int,
    acknowledged_warning_fingerprints: set[str],
    business_today,
) -> tuple[Order, list[RecalcResult]]:
    """Confirm (Plan v2 §6/§11, Final Architecture Lock §A, corrected by Independent
    Implementation Review Remediation Plan v1.1/v1.2 §1/§2): bounded-retry wrapper
    around `_confirm_order_attempt` — a full non-locking-preflight-through-commit
    cycle re-run from scratch whenever the candidate cross-Order set is found to
    have grown after Product is locked, exactly matching
    `production_run_service.finish_production`'s/`surplus_service`'s identical
    shape."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _confirm_order_attempt(
                db,
                business,
                order_id,
                expected_version=expected_version,
                acknowledged_warning_fingerprints=acknowledged_warning_fingerprints,
                business_today=business_today,
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "ORDER_LOCK_DISCOVERY_CONTENTION",
        "Too many concurrent changes affecting this order's demand; please retry.",
    )


def _confirm_order_attempt(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    *,
    expected_version: int,
    acknowledged_warning_fingerprints: set[str],
    business_today,
) -> tuple[Order, list[RecalcResult]]:
    """Lock -> validate -> lock operational graph -> mutate status/history +
    `touch_order` -> flush (not commit) -> recalculate -> warning review -> promote
    self + reevaluate every other candidate Order's readiness -> commit once, or
    roll back the entire attempt (leaving the Order `DRAFT`, zero operational rows)
    if warnings are unacknowledged, the candidate set grew, or recalculation fails.

    Locking (Remediation Plan v1.2 §1 — corrected from the self-first sequencing a
    naive reading of v1.1 might suggest): a non-locking preflight read of this
    Order's *currently persisted* lines seeds discovery of every OTHER
    CONFIRMED/READY Order that could be affected by this Product's recalculation;
    the directly-addressed Order id is unioned in explicitly (it's still `DRAFT` at
    discovery time, so `discover_candidate_order_ids`' own CONFIRMED/READY filter
    would otherwise miss it); the *entire* resulting set — self included — is then
    acquired in exactly one sorted `SELECT ... FOR UPDATE` phase, never a
    lock-self-then-lock-others sequence (which is not globally deadlock-safe: two
    transactions each confirming/editing/canceling a different Order that both
    reference the same Product could lock their own "self" first and then
    reciprocally wait on each other's self)."""
    preflight = order_service.get_order_for_business(db, order_id, business)
    preflight_products = {
        line.product_id for line in preflight.lines if line.product_id is not None
    }
    candidate_ids = _discover_full_order_set(db, business, order_id, preflight_products)
    locked_orders = _lock_orders_sorted(db, business, candidate_ids)
    order = locked_orders.get(order_id)
    if order is None:
        raise NotFoundError()

    results: list[RecalcResult] = []
    try:
        check_version(order, expected_version, resource="order")
        if order.status is not OrderStatus.DRAFT:
            raise ApiError(409, "ORDER_NOT_DRAFT", "Only a Draft order can be confirmed.")

        issues = order_pricing.check_structural_confirmation_readiness(
            status=order.status.value,
            line_count=len(order.lines),
            fulfillment_date_present=order.fulfillment_date is not None,
        )
        if issues:
            raise _structural_readiness_error(issues)

        # Phase 7 Final Lifecycle Invariant Correction Plan, Finding C — a supplied
        # fulfillment date+time must be a valid, unambiguous Business-local moment
        # regardless of Product type or Recipe elapsed-time availability.
        # `order.fulfillment_date` is guaranteed non-None by the structural check
        # above; `order.fulfillment_time` may legitimately still be None (a missing
        # time doesn't block confirmation, only warns elsewhere) — a no-op in that
        # case.
        validate_fulfillment_local_time(business, order.fulfillment_date, order.fulfillment_time)

        products = _acquire_operational_lock_graph(db, business, order)

        # Re-verify the candidate set under Product serialization (Remediation Plan
        # v1.2 §1 step 8) — if a new Order confirmed/edited for one of these
        # Products between the non-locking discovery above and this Product lock,
        # the locked set is incomplete; abandon this attempt entirely (never
        # escalate a partial lock) and let the outer wrapper retry from scratch.
        s1 = _discover_full_order_set(db, business, order_id, {p.id for p in products})
        if not s1.issubset(locked_orders.keys()):
            raise _CandidateOrderSetGrew()

        order.status = OrderStatus.CONFIRMED
        order.confirmed_at = datetime.now(UTC)
        db.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                from_status=OrderStatus.DRAFT,
                to_status=OrderStatus.CONFIRMED,
                changed_at=datetime.now(UTC),
            )
        )
        order_service.touch_order(order)
        db.flush()

        results = _recalculate_all(db, business, products, business_today=business_today)

        missing_fulfillment_time = order.fulfillment_time is None
        combined_ingredient_shortages = _combined_ingredient_shortages(db, business, results)
        fresh_fingerprints = _collect_warning_fingerprints(
            results,
            missing_fulfillment_time=missing_fulfillment_time,
            combined_ingredient_shortages=combined_ingredient_shortages,
        )
        if not fresh_fingerprints.issubset(acknowledged_warning_fingerprints):
            raise ApiError(
                422,
                "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT",
                "This order has operational warnings that must be reviewed before confirming.",
                issues=_warning_issues(
                    results,
                    missing_fulfillment_time=missing_fulfillment_time,
                    combined_ingredient_shortages=combined_ingredient_shortages,
                ),
            )

        # Cross-Order readiness (Independent Implementation Review Remediation Plan
        # v1/v1.1 §1/§2, Finding 2/3): this Order may itself now be fully satisfied
        # (e.g. by existing Surplus/Purchased stock) and every OTHER locked
        # candidate Order's satisfaction may have changed too, since this
        # confirmation's recalculation can compete for the same Product's Surplus/
        # Purchased capacity another Order relied on.
        readiness_service.evaluate_and_apply_readiness(db, business, order)
        for other_id, other_order in locked_orders.items():
            if other_id != order.id:
                readiness_service.evaluate_and_apply_readiness(db, business, other_order)
    except ApiError:
        db.rollback()
        raise

    commit_or_raise_stale(db, resource="order")
    return order, results


def update_confirmed_order(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    payload,
    *,
    expected_version: int,
    acknowledged_warning_fingerprints: set[str],
    business_today,
) -> tuple[Order, list[RecalcResult]]:
    """Bounded-retry wrapper around `_update_confirmed_order_attempt` — see
    `confirm_order`'s identical wrapper for the shared rationale (Independent
    Implementation Review Remediation Plan v1.1/v1.2 §1)."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _update_confirmed_order_attempt(
                db,
                business,
                order_id,
                payload,
                expected_version=expected_version,
                acknowledged_warning_fingerprints=acknowledged_warning_fingerprints,
                business_today=business_today,
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "ORDER_LOCK_DISCOVERY_CONTENTION",
        "Too many concurrent changes affecting this order's demand; please retry.",
    )


def _update_confirmed_order_attempt(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    payload,
    *,
    expected_version: int,
    acknowledged_warning_fingerprints: set[str],
    business_today,
) -> tuple[Order, list[RecalcResult]]:
    """Confirmed-Order editing (Phase 7 Implementation Remediation Plan, Finding 1,
    completed by the Phase 7 Final Remediation Correction Plan, Finding 1; Plan v2
    §6/§11; Final Architecture Lock §A/§B/§C/§F; corrected by Independent
    Implementation Review Remediation Plan v1.1/v1.2 §1/§2): non-locking preflight
    (this Order's currently-persisted Products UNION a best-effort resolution of the
    proposed payload's Products/Selling-Options — Remediation Plan v1.2 §1's
    correction: the old set alone misses a line being moved TO a new Product) ->
    discover every other candidate Order -> lock the ENTIRE Order set (self
    included) in one sorted phase -> revalidate self under lock -> reject an edit
    that would alter protected operational demand (per-line, §C) -> Phase A
    (`order_service._prepare_order_edit`) validates/locks Customer + the REAL,
    authoritative operational Product superset + Selling Options in ONE pass, with
    NO Order/OrderLine mutation yet -> Recipe/Ingredient/Surplus locks for every
    affected Product -> re-verify the candidate Order set under Product
    serialization, retry the whole attempt if grown -> Phase B
    (`order_service._apply_order_edit_body`) applies the complete proposed
    Order/OrderLine state (never a partial patch) using Phase A's already-locked
    references -> `touch_order()` -> flush (not commit) -> recalculate every affected
    Product's closure, only when the edit is demand-affecting (Plan v2 §11 table —
    price/notes/customer-financial-only edits never recalculate, never lock/discover
    beyond the Order-set phase) -> fresh warning review using the same fingerprint-
    acknowledgement protocol confirmation itself uses (§F) -> promote/demote self and
    every other locked Order's readiness -> commit once, or roll back the ENTIRE edit
    if recalculation, warning review, or candidate-set verification fails."""
    preflight = order_service.get_order_for_business(db, order_id, business)
    preflight_old_product_ids = {
        line.product_id for line in preflight.lines if line.product_id is not None
    }
    preflight_new_product_ids = _preflight_resolve_payload_product_ids(db, business, payload)
    candidate_ids = _discover_full_order_set(
        db, business, order_id, preflight_old_product_ids | preflight_new_product_ids
    )
    locked_orders = _lock_orders_sorted(db, business, candidate_ids)
    order = locked_orders.get(order_id)
    if order is None:
        raise NotFoundError()

    results: list[RecalcResult] = []
    try:
        check_version(order, expected_version, resource="order")
        if order.status is not OrderStatus.CONFIRMED:
            raise ApiError(
                409, "ORDER_NOT_CONFIRMED", "Only a Confirmed order can be edited this way."
            )

        # Phase 7 Final Lifecycle Invariant Correction Plan, Finding A — a persisted
        # CONFIRMED Order must never end an edit in a state that could not pass
        # structural confirmation (zero lines, no fulfillment date).
        # `OrderConfirmedEditRequest` legally permits both in its payload shape (it
        # reuses the Draft-edit schema), so this must be checked explicitly. Runs
        # BEFORE the active-run/protected-demand check below: a structurally invalid
        # candidate is rejected as `ORDER_NOT_CONFIRMABLE`, never masked by
        # `ORDER_PRODUCTION_LOCKED`.
        issues = order_pricing.check_candidate_structural_readiness(
            line_count=len(payload.lines),
            fulfillment_date_present=payload.fulfillment_date is not None,
        )
        if issues:
            raise _structural_readiness_error(issues)

        # Must run against the Order's CURRENT (pre-mutation) line/header state.
        _reject_if_confirmed_edit_touches_protected_demand(db, business, order, payload)
        demand_affecting = _is_demand_affecting_edit(order, payload)

        # Phase 7 Final Lifecycle Invariant Correction Plan, Finding C — validated
        # only when the proposed fulfillment date/time itself changed, or the edit is
        # otherwise demand-affecting and will enter operational recalculation. NEVER
        # unconditionally: a genuinely non-operational edit (price-only, notes-only,
        # customer/financial-only) must remain allowed even if the Order's
        # ALREADY-STORED fulfillment time has since become DST-invalid under a
        # subsequently-changed `Business.timezone` — the recalculation layer below
        # remains the defense-in-depth backstop for anything that actually
        # recalculates demand.
        fulfillment_changed = (
            payload.fulfillment_date != order.fulfillment_date
            or payload.fulfillment_time != order.fulfillment_time
        )
        if fulfillment_changed or demand_affecting:
            validate_fulfillment_local_time(
                business, payload.fulfillment_date, payload.fulfillment_time
            )

        old_product_ids = frozenset(
            line.product_id for line in order.lines if line.product_id is not None
        )
        new_product_ids = frozenset(
            line.product_id for line in payload.lines if line.product_id is not None
        )
        # Amendment §6's composed ordering: the operational Product superset is only
        # relevant (and only locked) when the edit actually needs recalculation —
        # a price/notes-only edit locks no more than Phase 6 already would. This is
        # the REAL, authoritative superset (computed from the now-locked Order and
        # the payload together) — the preflight union above was only ever a
        # discovery-time approximation used to decide who to lock.
        additional_product_ids = (
            (old_product_ids | new_product_ids) if demand_affecting else (frozenset())
        )

        (
            products_by_id,
            selling_options,
            existing_lines_by_id,
            submitted_ids,
            payments_total,
        ) = order_service._prepare_order_edit(
            db, business, order, payload, additional_product_ids=additional_product_ids
        )

        # Recipe/Ingredient/Surplus locks are acquired here — AFTER reference
        # validation/locking (Phase A above) but BEFORE any Order/OrderLine mutation
        # (Phase B below) — so the full composed lock graph (Final Architecture Lock
        # §B) is complete, by construction, before a single write is emitted for this
        # edit. This does not rely on the session's `autoflush=False` setting to keep
        # Phase B's mutation from reaching PostgreSQL early (Finding 1 of the Phase 7
        # Final Remediation Correction Plan).
        affected_products: list[Product] = []
        if demand_affecting:
            affected_products = [
                products_by_id[pid]
                for pid in sorted(additional_product_ids)
                if pid in products_by_id
            ]
            _acquire_recipe_and_ingredient_locks(db, business, affected_products)
            _acquire_surplus_and_purchased_locks(db, business, affected_products)

            # Re-verify the candidate set under Product serialization (Remediation
            # Plan v1.2 §1 step 8), now using the REAL affected-Product set rather
            # than the preflight approximation — if it grew (a new Order confirmed
            # against one of these Products, or the payload's real resolution
            # touches a Product the preflight missed), abandon this attempt entirely
            # and let the outer wrapper retry from a clean, fully-unlocked state.
            s1 = _discover_full_order_set(db, business, order_id, {p.id for p in affected_products})
            if not s1.issubset(locked_orders.keys()):
                raise _CandidateOrderSetGrew()

        order_service._apply_order_edit_body(
            db,
            business,
            order,
            payload,
            products=products_by_id,
            selling_options=selling_options,
            existing_lines_by_id=existing_lines_by_id,
            submitted_ids=submitted_ids,
            payments_total=payments_total,
        )

        order_service.touch_order(order)
        db.flush()

        if demand_affecting:
            results = _recalculate_all(
                db, business, affected_products, business_today=business_today
            )
            missing_fulfillment_time = order.fulfillment_time is None
            combined_ingredient_shortages = _combined_ingredient_shortages(db, business, results)
            fresh_fingerprints = _collect_warning_fingerprints(
                results,
                missing_fulfillment_time=missing_fulfillment_time,
                combined_ingredient_shortages=combined_ingredient_shortages,
            )
            if not fresh_fingerprints.issubset(acknowledged_warning_fingerprints):
                raise ApiError(
                    422,
                    "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT",
                    "This order has operational warnings that must be reviewed before saving.",
                    issues=_warning_issues(
                        results,
                        missing_fulfillment_time=missing_fulfillment_time,
                        combined_ingredient_shortages=combined_ingredient_shortages,
                    ),
                )

            # Cross-Order readiness (Independent Implementation Review Remediation
            # Plan v1/v1.1 §1/§2, Finding 2/3) — only meaningful when recalculation
            # actually ran; a non-demand-affecting edit changes nothing any Order's
            # satisfaction could depend on.
            readiness_service.evaluate_and_apply_readiness(db, business, order)
            for other_id, other_order in locked_orders.items():
                if other_id != order.id:
                    readiness_service.evaluate_and_apply_readiness(db, business, other_order)
    except ApiError:
        db.rollback()
        raise

    commit_or_raise_stale(db, resource="order")
    return order, results


def _order_completed_production_allocation_totals(
    db: Session, business: Business, order: Order
) -> tuple[Decimal, Decimal]:
    """Sum of completed `ProductionRunOrderAllocation` quantity and `PRODUCTION`-type
    historical cost already attributed to this Order's lines — the exact figures the
    post-production cancellation warning fingerprint depends on."""
    line_ids = [line.id for line in order.lines]
    if not line_ids:
        return Decimal(0), Decimal(0)
    total_quantity = db.scalar(
        select(func.coalesce(func.sum(ProductionRunOrderAllocation.quantity), 0)).where(
            ProductionRunOrderAllocation.order_line_id.in_(line_ids),
            ProductionRunOrderAllocation.business_id == business.id,
        )
    )
    total_cost = db.scalar(
        select(func.coalesce(func.sum(OrderCostAllocation.amount), 0)).where(
            OrderCostAllocation.order_id == order.id,
            OrderCostAllocation.cost_type == CostType.PRODUCTION,
            OrderCostAllocation.business_id == business.id,
        )
    )
    return total_quantity, total_cost


def _cancellation_after_production_fingerprint(
    order: Order, *, total_quantity: Decimal, total_cost: Decimal
) -> str:
    """Deterministic content-hash over exactly the authoritative, freshly-recomputed
    consequence state (Phase 8 Final Architecture Lock Addendum §A, reusing the
    RUN-004/ADR-128 fingerprint pattern) — never a client-supplied value, always
    recomputed fresh under the Order lock on every attempt."""
    payload = f"{order.id}:{total_quantity}:{total_cost}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _reject_if_cancellation_after_production_unacknowledged(
    db: Session, business: Business, order: Order, *, acknowledged_warning_fingerprint: str | None
) -> None:
    """The two-tier Cancel rule for CONFIRMED/READY (Phase 8 Final Architecture Lock
    Addendum §A): an `IN_PRODUCTION`-covered line still hard-blocks Cancel entirely
    (`_reject_if_production_locked`, called separately, unchanged). A line covered
    only by COMPLETED, historical production (no active run remains) does not block
    Cancel — it requires explicit warning acknowledgment first, mirroring RUN-004's
    fingerprint protocol exactly."""
    total_quantity, total_cost = _order_completed_production_allocation_totals(db, business, order)
    if total_quantity <= 0:
        return

    fingerprint = _cancellation_after_production_fingerprint(
        order, total_quantity=total_quantity, total_cost=total_cost
    )
    if acknowledged_warning_fingerprint == fingerprint:
        return

    raise ApiError(
        409,
        "CANCELLATION_AFTER_PRODUCTION_REQUIRES_ACKNOWLEDGMENT",
        "This order has completed production history. Review and acknowledge before canceling.",
        issues=[
            {
                "severity": "WARNING",
                "code": "CANCELLATION_AFTER_PRODUCTION",
                "message": (
                    f"{total_quantity} units already produced for this order "
                    f"(historical cost {total_cost}) will remain a permanent, immutable "
                    "record. Ingredients already consumed will not be restored."
                ),
                "field": None,
                "resource": "order",
                "details": {
                    "produced_quantity": str(total_quantity),
                    "historical_cost": str(total_cost),
                    "fingerprint": fingerprint,
                },
            }
        ],
    )


def cancel_confirmed_order(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    *,
    expected_version: int,
    business_today,
    acknowledged_warning_fingerprint: str | None = None,
) -> tuple[Order, list[RecalcResult]]:
    """Bounded-retry wrapper around `_cancel_confirmed_order_attempt` — see
    `confirm_order`'s identical wrapper for the shared rationale (Independent
    Implementation Review Remediation Plan v1.1/v1.2 §1)."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _cancel_confirmed_order_attempt(
                db,
                business,
                order_id,
                expected_version=expected_version,
                business_today=business_today,
                acknowledged_warning_fingerprint=acknowledged_warning_fingerprint,
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "ORDER_LOCK_DISCOVERY_CONTENTION",
        "Too many concurrent changes affecting this order's demand; please retry.",
    )


def _cancel_confirmed_order_attempt(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    *,
    expected_version: int,
    business_today,
    acknowledged_warning_fingerprint: str | None = None,
) -> tuple[Order, list[RecalcResult]]:
    """Cancel (Plan v2 §11, corrected by Final Architecture Lock §A/§C, Final
    Architecture Lock Addendum §A, and Independent Implementation Review Remediation
    Plan v1.1/v1.2 §1): eligible for `CONFIRMED` and `READY` alike (READY is still
    pre-Completion). A line still covered by an `IN_PRODUCTION` run hard-blocks
    Cancel entirely, unchanged. A line covered only by completed, historical
    production requires explicit warning acknowledgment — Cancel then proceeds
    without restoring consumed Ingredients, without rewriting any historical
    `ProductionRun`/`ProductionRunOrderAllocation`/`OrderCostAllocation` row, and
    without manufacturing a new Surplus lot from the now-unclaimed output.

    **May change another Order's readiness** (Remediation Plan v1/v1.1 §1/§2,
    Finding 2/3): canceling releases this Order's own Purchased reservation/Surplus
    allocation claims, which can newly satisfy a sibling Order's previously-unmet
    demand for the same Product. Locking therefore follows the same single
    globally-sorted Order-lock phase `confirm_order`/`update_confirmed_order` use —
    a non-locking preflight discovers every other CONFIRMED/READY Order referencing
    this Order's currently-persisted Products, the entire set (self included) is
    locked in one sorted phase, and every other locked Order's readiness is
    reevaluated after this Order's recalculation-triggered release, all within the
    same transaction Cancel itself commits."""
    preflight = order_service.get_order_for_business(db, order_id, business)
    preflight_products = {
        line.product_id for line in preflight.lines if line.product_id is not None
    }
    candidate_ids = _discover_full_order_set(db, business, order_id, preflight_products)
    locked_orders = _lock_orders_sorted(db, business, candidate_ids)
    order = locked_orders.get(order_id)
    if order is None:
        raise NotFoundError()

    results: list[RecalcResult] = []
    try:
        check_version(order, expected_version, resource="order")
        if order.status not in (OrderStatus.CONFIRMED, OrderStatus.READY):
            raise ApiError(
                409, "ORDER_NOT_CANCELABLE", "Only a Confirmed or Ready order can be canceled."
            )

        _reject_if_production_locked(db, business, order)
        _reject_if_cancellation_after_production_unacknowledged(
            db, business, order, acknowledged_warning_fingerprint=acknowledged_warning_fingerprint
        )
        products = _acquire_operational_lock_graph(db, business, order)

        # Re-verify the candidate set under Product serialization (Remediation Plan
        # v1.2 §1 step 8) — mirrors `confirm_order`'s identical step.
        s1 = _discover_full_order_set(db, business, order_id, {p.id for p in products})
        if not s1.issubset(locked_orders.keys()):
            raise _CandidateOrderSetGrew()

        from_status = order.status
        order.status = OrderStatus.CANCELED
        order.canceled_at = datetime.now(UTC)
        order.ready_at = None
        db.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                from_status=from_status,
                to_status=OrderStatus.CANCELED,
                changed_at=datetime.now(UTC),
            )
        )
        order_service.touch_order(order)
        db.flush()

        # The Order's own status is already CANCELED (flushed) and its lines no
        # longer count as CONFIRMED/READY demand for `recalculate_product_closure`'s
        # own query — this recalculation pass naturally rebuilds each affected
        # Product's closure with this Order's demand absent, which is also what
        # releases this order's now-stale PurchasedProductReservation/
        # SurplusAllocation rows (both are rebuildable, since a COMPLETED run's
        # source requirement is no longer IN_PRODUCTION-protected) — no separate,
        # explicit release step is needed. Historical ProductionRun/
        # ProductionRunOrderAllocation/OrderCostAllocation rows are untouched by
        # this pass entirely (recalculation never reads or writes them).
        results = _recalculate_all(db, business, products, business_today=business_today)

        # Cross-Order readiness (Remediation Plan v1/v1.1 §1/§2, Finding 2/3): this
        # Order is terminal (CANCELED), so no self-promotion is possible, but every
        # OTHER locked candidate Order's satisfaction may have changed now that this
        # Order's claims are released.
        for other_id, other_order in locked_orders.items():
            if other_id != order.id:
                readiness_service.evaluate_and_apply_readiness(db, business, other_order)
    except ApiError:
        db.rollback()
        raise

    commit_or_raise_stale(db, resource="order")
    return order, results


def _combined_ingredient_shortages(
    db: Session, business: Business, results: list[RecalcResult]
) -> dict[uuid.UUID, IngredientShortageResult]:
    """Phase 7 Final Remediation Correction Plan, Finding 5 — a real Confirm/
    confirmed-edit that recalculates several Products in one request runs each
    Product's own `recalculate_product_closure` call sequentially, `persist=True`,
    flushing its fresh reservations before the NEXT affected Product's own call
    runs. So a Product processed EARLY in this loop has its own
    `ingredient_shortages` entry computed BEFORE a later sibling Product's
    contribution to the same shared Ingredient was flushed — stale by the time
    every Product is done — while a Product processed LATE sees every earlier
    sibling's contribution correctly, but two different Products' own entries for
    the SAME Ingredient can therefore disagree, and since the fingerprint string
    embeds the exact shortage quantity, naively iterating each result's own list
    can legitimately mint two different fingerprints for what is really one
    shared Ingredient's one final shortage.

    Once every affected Product's `recalculate_product_closure(persist=True)` call
    has completed (this function is only ever called after `_recalculate_all`
    returns), every Product's true final contribution is ALREADY genuine,
    committed-within-this-transaction `IngredientReservation` state — unlike
    Preview's `persist=False` case (`order_preview_service._combined_shortages_
    for_results`), which must manually exclude stale not-yet-deleted rows and add
    back each Product's own in-memory candidate total, there is nothing left to
    reconstruct here: a single authoritative full read (no exclusion, no
    synthesized extra candidate) already reflects every affected Product's
    combined demand for a shared Ingredient exactly once."""
    ingredient_ids: set[uuid.UUID] = set()
    for result in results:
        ingredient_ids |= {shortage.ingredient_id for shortage in result.ingredient_shortages}
    return compute_combined_ingredient_shortages(db, business, ingredient_ids)


_MISSING_FULFILLMENT_TIME_FINGERPRINT = "MISSING_FULFILLMENT_TIME"
"""A constant, not per-Order-id, fingerprint (Phase 7 Implementation Remediation
Plan, Finding 4/amendment 1): `fulfillment_time` is a single Order-level field
(`order.py:77`), so a confirm/edit/preview request is always scoped to exactly one
Order's worth of this warning — unlike MISSING_RECIPE/INGREDIENT_SHORTAGE, which are
Product/Ingredient-scoped and can repeat within one Order. Using the Order's own id
in the fingerprint would make it unstable between a new-Order Preview (no id yet)
and its subsequent Confirm (a freshly assigned id) — this constant token avoids that
mismatch entirely, in both directions."""


def _collect_warning_fingerprints(
    results: list[RecalcResult],
    *,
    missing_fulfillment_time: bool,
    combined_ingredient_shortages: dict[uuid.UUID, IngredientShortageResult],
) -> set[str]:
    """`combined_ingredient_shortages` (Finding 5) is the single authoritative
    per-Ingredient read across every affected Product in this request — see
    `_combined_ingredient_shortages` — never each result's own independently-
    computed `ingredient_shortages` list, which could legitimately disagree with a
    sibling Product's own entry for the same shared Ingredient."""
    fingerprints: set[str] = set()
    if missing_fulfillment_time:
        fingerprints.add(_MISSING_FULFILLMENT_TIME_FINGERPRINT)
    for result in results:
        if result.missing_recipe:
            fingerprints.add(f"MISSING_RECIPE:{result.product_id}")
        if result.purchased_shortage_quantity and result.purchased_shortage_quantity > 0:
            fingerprints.add(
                f"PURCHASED_SHORTAGE:{result.product_id}:{result.purchased_shortage_quantity}"
            )
    for shortage in combined_ingredient_shortages.values():
        if shortage.shortage_quantity > 0:
            fingerprints.add(
                f"INGREDIENT_SHORTAGE:{shortage.ingredient_id}:{shortage.shortage_quantity}"
            )
    return fingerprints


def _warning_issues(
    results: list[RecalcResult],
    *,
    missing_fulfillment_time: bool,
    combined_ingredient_shortages: dict[uuid.UUID, IngredientShortageResult],
) -> list[dict]:
    """Each issue's `details.fingerprint` is the exact opaque token the frontend
    should echo back, unmodified, in a subsequent `OrderConfirmRequest`'s
    `acknowledged_warning_fingerprints` — the frontend never reconstructs a
    fingerprint itself; fingerprint construction stays entirely backend-owned.

    `missing_fulfillment_time` is an ORDER-level fact (Finding 4/amendment 1): it
    fires exactly once per Order/hypothetical-order regardless of line composition
    (Produced, Purchased, or Custom-Item-only) or whether recalculation ultimately
    finds any new production is required — never derived per-`ProductionRequirement`
    the way the other warnings below are.

    `combined_ingredient_shortages` (Finding 5): see `_collect_warning_fingerprints`
    — the `missing_recipe`/`purchased_shortage_quantity` dimensions stay correctly
    per-`result` (Product-scoped, unaffected by Finding 5's shared-Ingredient
    correction); only the Ingredient dimension reads from this combined dict."""
    issues: list[dict] = []
    if missing_fulfillment_time:
        issues.append(
            {
                "severity": "WARNING",
                "code": "MISSING_FULFILLMENT_TIME",
                "message": (
                    "This order has no fulfillment time set. Production timing guidance "
                    "cannot be precisely calculated without one."
                ),
                "field": "fulfillment_time",
                "resource": "order",
                "details": {"fingerprint": _MISSING_FULFILLMENT_TIME_FINGERPRINT},
            }
        )
    for result in results:
        if result.missing_recipe:
            issues.append(
                {
                    "severity": "WARNING",
                    "code": "PRODUCT_MISSING_RECIPE",
                    "message": (
                        "This product has no applicable recipe. Any portion of its demand "
                        "not covered by existing surplus cannot be automatically planned."
                    ),
                    "field": None,
                    "resource": "product",
                    "details": {
                        "product_id": str(result.product_id),
                        "fingerprint": f"MISSING_RECIPE:{result.product_id}",
                    },
                }
            )
        if result.purchased_shortage_quantity and result.purchased_shortage_quantity > 0:
            issues.append(
                {
                    "severity": "WARNING",
                    "code": "PURCHASED_PRODUCT_SHORTAGE",
                    "message": "There is not enough purchased stock to cover demand.",
                    "field": None,
                    "resource": "product",
                    "details": {
                        "product_id": str(result.product_id),
                        "shortage_quantity": str(result.purchased_shortage_quantity),
                        "fingerprint": (
                            f"PURCHASED_SHORTAGE:{result.product_id}:"
                            f"{result.purchased_shortage_quantity}"
                        ),
                    },
                }
            )
    for shortage in combined_ingredient_shortages.values():
        if shortage.shortage_quantity > 0:
            issues.append(
                {
                    "severity": "WARNING",
                    "code": "INGREDIENT_SHORTAGE",
                    "message": (
                        f"There is not enough {shortage.ingredient_name} "
                        f"({shortage.canonical_unit}) to cover demand."
                    ),
                    "field": None,
                    "resource": "ingredient",
                    "details": {
                        "ingredient_id": str(shortage.ingredient_id),
                        "shortage_quantity": str(shortage.shortage_quantity),
                        "fingerprint": (
                            f"INGREDIENT_SHORTAGE:{shortage.ingredient_id}:"
                            f"{shortage.shortage_quantity}"
                        ),
                    },
                }
            )
    return issues


def set_custom_item_manual_fulfillment(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    order_line_id: uuid.UUID,
    *,
    expected_version: int,
    satisfied: bool,
) -> Order:
    """The smallest explicit seller-controlled action for a one-off `CUSTOM_ITEM`
    line's `manual_fulfillment_satisfied` flag (Independent Implementation Review
    Remediation Plan v1/v1.2, Finding 4) — `readiness_service` already correctly
    requires this flag before a Custom Item line counts as satisfied, but no prior
    application path ever let a seller set it (only direct ORM assignment in
    tests). Deliberately NOT routed through arbitrary Order editing: a `CUSTOM_ITEM`
    line never creates Produced/Purchased demand (ORD-009/010,
    `_line_is_demand_affecting_change`'s own documented rule), so this mutation has
    no cross-Product, cross-Order, or Recipe/Ingredient/Surplus effect at all —
    locking self (this Order) is sufficient, no operational lock graph, no
    discover-lock-verify-retry protocol needed.

    Only `CONFIRMED`/`READY` (both pre-Completion, both where READY evaluation is
    meaningful) may be mutated; `DRAFT` has no readiness concept yet and
    `COMPLETED`/`CANCELED` are immutable history. Only a `CUSTOM_ITEM` line with
    `manual_fulfillment_required` set may be mutated — this flag has no meaning
    otherwise. Participates in the Order's own optimistic-concurrency version
    (`touch_order`), and triggers whole-Order readiness evaluation atomically:
    setting `satisfied=True` may promote CONFIRMED->READY if this was the last
    unmet line; clearing it on an already-READY Order demotes READY->CONFIRMED,
    exactly like any other authoritative supply-loss event."""
    order = order_service.get_order_for_business_locked(db, order_id, business)
    try:
        check_version(order, expected_version, resource="order")
        if order.status not in (OrderStatus.CONFIRMED, OrderStatus.READY):
            raise ApiError(
                409,
                "ORDER_NOT_ELIGIBLE_FOR_MANUAL_FULFILLMENT",
                "Only a Confirmed or Ready order's line can be marked "
                "satisfied/unsatisfied this way.",
            )

        line = next((line for line in order.lines if line.id == order_line_id), None)
        if line is None:
            raise NotFoundError()
        if line.line_type is not OrderLineType.CUSTOM_ITEM or not line.manual_fulfillment_required:
            raise ApiError(
                422,
                "LINE_NOT_MANUAL_FULFILLMENT",
                "This line is not a Custom Item requiring manual fulfillment.",
            )

        line.manual_fulfillment_satisfied = satisfied
        order_service.touch_order(order)
        db.flush()

        readiness_service.evaluate_and_apply_readiness(db, business, order)
    except ApiError:
        db.rollback()
        raise

    commit_or_raise_stale(db, resource="order")
    return order
