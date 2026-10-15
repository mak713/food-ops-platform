"""Manual Surplus creation and adjustment (SUR-008; Phase 8 Final Architecture Lock
§9, corrected by Final Architecture Amendment §3 and Final Architecture Lock
Addendum §B).

Manual creation (`create_surplus_manual`) always produces a lot with
`source_production_run_id = source_total_cost_basis = NULL` — the seller-required
`unit_cost_basis` is the sole authoritative valuation for such a lot, never silently
defaulted. `POST /surplus/{id}/adjust` (`adjust_surplus`) is reduction-only: new
positive supply always goes through creation, never an in-place top-up (blending a
manually-added quantity into a run-sourced lot would leave `source_total_cost_basis`
describing only part of the lot).

Both mutation paths reuse the same discover-lock-verify-retry protocol
`production_run_service.finish_production` and `purchased_inventory_service`
establish — a positive creation (or a reduction that stays within unallocated
slack) never blocks; a reduction below the lot's currently-allocated quantity is a
consequential reconciliation transaction, gated by the same RUN-004-style warning-
fingerprint acknowledgment protocol, that reuses the existing, already-tested FEFO
Surplus allocator (via `recalculate_product_closure`) rather than inventing a new
manual release-priority algorithm.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.tenant import check_version, commit_or_raise_stale, get_owned_or_404
from app.db.enums import OrderStatus, ProductType, SurplusTransactionType
from app.db.models.business import Business
from app.db.models.order import Order
from app.db.models.product import Product
from app.db.models.surplus import SurplusAllocation, SurplusInventory, SurplusTransaction
from app.domain.inventory_costing import is_representable_in_numeric_18_6, quantize_for_storage
from app.schemas.surplus import SurplusAdjustmentRequest, SurplusManualCreateRequest
from app.services import order_lifecycle_service, readiness_service
from app.services.operational_recalculation_service import (
    get_protected_requirement_ids,
    recalculate_product_closure,
    reconcile_ready_for_lots,
)
from app.services.product_service import get_product_for_business, get_product_for_business_locked

_MAX_DISCOVERY_RETRIES = 3
_OVERFLOW_CODE = "SURPLUS_VALUE_OVERFLOW"


class _CandidateOrderSetGrew(Exception):
    """Internal-only retry signal — see `production_run_service`'s identical pattern."""


def _persist_quantity(value: Decimal, *, field: str) -> Decimal:
    quantized = quantize_for_storage(value)
    if not is_representable_in_numeric_18_6(quantized):
        raise ApiError(422, _OVERFLOW_CODE, f"{field} is too large to store.")
    return quantized


def _lock_full_graph_for_surplus_mutation(
    db: Session, business: Business, product_id: uuid.UUID
) -> tuple[Product, dict[uuid.UUID, Order], list[SurplusInventory]]:
    """The complete `Order -> Product -> Recipe/Ingredient -> Surplus` lock graph,
    always acquired in full (Independent Implementation Review Remediation Plan
    v1.2 §2/§4/§5/§6/§7): locking is never conditional on the initial candidate
    discovery being non-empty, and never skipped in favor of an unlocked read —
    only the WORK performed after this point (whether to recalculate at all,
    whether a within-slack fast path applies) may ever be skipped. Reuses
    `order_lifecycle_service`'s Recipe/Ingredient lock helper (the same one
    `production_run_service.py` already reuses for the identical purpose) rather
    than duplicating a second interpretation of the graph; the Surplus-lot lock is
    a local query so the locked rows themselves are returned (needed for the
    all-lot defensive invariant check `adjust_surplus` performs)."""
    candidate_ids_s0 = readiness_service.discover_candidate_order_ids(db, business, product_id)
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

    product = get_product_for_business_locked(db, product_id, business)
    order_lifecycle_service._acquire_recipe_and_ingredient_locks(db, business, [product])
    locked_lots = list(
        db.scalars(
            select(SurplusInventory)
            .where(
                SurplusInventory.product_id == product.id,
                SurplusInventory.business_id == business.id,
            )
            .order_by(SurplusInventory.id)
            .with_for_update()
        )
    )

    candidate_ids_s1 = readiness_service.discover_candidate_order_ids(db, business, product_id)
    if not set(candidate_ids_s1).issubset(locked_orders.keys()):
        raise _CandidateOrderSetGrew()

    return product, locked_orders, locked_lots


def _active_run_fixed_quantity_for_lot(
    db: Session,
    business: Business,
    lot_id: uuid.UUID,
    protected_requirement_ids: set[uuid.UUID],
) -> Decimal:
    """The portion of `lot_id`'s active allocation that is fixed because it belongs
    to an `IN_PRODUCTION`-protected `ProductionRequirement` (Remediation Plan v1.2
    §3) — a hard structural floor a manual Surplus reduction may never cross. This
    is not the same code path as the READY-demotion acknowledgment gate: READY
    promises may be reconciled after explicit consequence acknowledgment, but
    `IN_PRODUCTION`-protected demand cannot be rewritten by this action at all —
    the only sanctioned way to change it is to finish or cancel the run itself."""
    if not protected_requirement_ids:
        return Decimal(0)
    return db.scalar(
        select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
            SurplusAllocation.surplus_inventory_id == lot_id,
            SurplusAllocation.production_requirement_id.in_(protected_requirement_ids),
            SurplusAllocation.business_id == business.id,
        )
    )


def _active_allocation_by_line_for_orders(
    db: Session, business: Business, order_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Every active `SurplusAllocation` quantity, summed per `OrderLine`, across the
    given Orders — ALL active allocations, not only READY-linked ones (Remediation
    Plan v1.2 §4's acknowledgment guard): a currently-CONFIRMED Order losing an
    existing Surplus allocation is a consequential change even when no READY Order
    is demoted."""
    order_ids = list(order_ids)
    if not order_ids:
        return {}
    rows = db.execute(
        select(SurplusAllocation.order_line_id, func.sum(SurplusAllocation.quantity))
        .where(
            SurplusAllocation.order_id.in_(order_ids),
            SurplusAllocation.business_id == business.id,
        )
        .group_by(SurplusAllocation.order_line_id)
    ).all()
    return dict(rows)


def _business_local_date(dt: datetime, business: Business) -> date:
    """Business-local calendar date, mirroring ADR-129's `business_today` pattern —
    never a raw UTC `.date()` (Phase 8 Final Architecture Lock §9)."""
    return dt.astimezone(ZoneInfo(business.timezone)).date()


def create_surplus_manual(
    db: Session,
    business: Business,
    payload: SurplusManualCreateRequest,
    *,
    business_today: date,
) -> SurplusInventory:
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _create_surplus_manual_attempt(
                db, business, payload, business_today=business_today
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "SURPLUS_CREATE_CONTENTION",
        "Too many concurrent changes to this product's demand; please retry.",
    )


def _create_surplus_manual_attempt(
    db: Session,
    business: Business,
    payload: SurplusManualCreateRequest,
    *,
    business_today: date,
) -> SurplusInventory:
    unlocked_product = get_product_for_business(db, payload.product_id, business)
    if unlocked_product.product_type is not ProductType.PRODUCED:
        raise ApiError(
            422,
            "SURPLUS_REQUIRES_PRODUCED_PRODUCT",
            "Only Produced products can have Surplus inventory.",
        )

    product, locked_orders, _locked_lots = _lock_full_graph_for_surplus_mutation(
        db, business, unlocked_product.id
    )

    produced_at = payload.produced_at or datetime.now(UTC)
    usable_through_date = payload.usable_through_date
    if usable_through_date is None and product.default_surplus_usable_days is not None:
        usable_through_date = _business_local_date(produced_at, business) + timedelta(
            days=product.default_surplus_usable_days
        )

    surplus_row = SurplusInventory(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        source_production_run_id=None,
        physical_quantity=_persist_quantity(payload.quantity, field="quantity"),
        unit_cost_basis=_persist_quantity(payload.unit_cost_basis, field="unit_cost_basis"),
        source_total_cost_basis=None,
        produced_at=produced_at,
        usable_through_date=usable_through_date,
        is_reusable=(
            payload.is_reusable if payload.is_reusable is not None else product.can_reuse_surplus
        ),
        notes=payload.notes,
    )
    db.add(surplus_row)
    db.flush()
    db.add(
        SurplusTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            surplus_inventory_id=surplus_row.id,
            transaction_type=SurplusTransactionType.MANUAL_ADJUSTMENT,
            quantity_change=surplus_row.physical_quantity,
            reason="Manual Surplus creation",
            notes=payload.notes,
        )
    )

    if locked_orders:
        db.flush()
        recalculate_product_closure(db, business, product, business_today=business_today)
        for order in locked_orders.values():
            readiness_service.evaluate_and_apply_readiness(db, business, order)

    db.commit()
    return surplus_row


def _active_allocated_quantity(
    db: Session, business: Business, surplus_inventory_id: uuid.UUID
) -> Decimal:
    return db.scalar(
        select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
            SurplusAllocation.surplus_inventory_id == surplus_inventory_id,
            SurplusAllocation.business_id == business.id,
        )
    )


def _surplus_reduction_fingerprint(
    surplus_inventory_id: uuid.UUID,
    *,
    new_physical_quantity: Decimal,
    demoted_order_ids: list[uuid.UUID],
    reduced_line_ids: list[uuid.UUID],
) -> str:
    """Encodes BOTH consequence dimensions (Remediation Plan v1.2 §4's
    acknowledgment guard): a whole-Order READY->CONFIRMED demotion, and any
    OrderLine whose active Surplus allocation quantity was reduced at all — a
    currently-CONFIRMED Order losing part of an existing allocation is
    consequential even when no READY Order is demoted. A changed consequence set
    (either dimension) yields a different fingerprint, automatically invalidating
    a stale acknowledgment."""
    sorted_demoted = sorted(str(i) for i in demoted_order_ids)
    sorted_reduced = sorted(str(i) for i in reduced_line_ids)
    payload = f"{surplus_inventory_id}:{new_physical_quantity}:{sorted_demoted}:{sorted_reduced}"
    return hashlib.sha256(payload.encode()).hexdigest()


def adjust_surplus(
    db: Session,
    business: Business,
    surplus_id: uuid.UUID,
    payload: SurplusAdjustmentRequest,
    *,
    business_today: date,
) -> SurplusInventory:
    """Reduction-only (`payload.quantity_delta < 0`, enforced by the schema). Within
    unallocated slack: cheap single-lot mutation. Below the lot's currently-allocated
    quantity: a consequential reconciliation transaction (Phase 8 Final Architecture
    Lock Addendum §B) — never a silent fabrication, never a committed state where
    promised Surplus exceeds physical Surplus."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _adjust_surplus_attempt(
                db, business, surplus_id, payload, business_today=business_today
            )
        except _CandidateOrderSetGrew:
            db.rollback()
            continue
    raise ApiError(
        409,
        "SURPLUS_ADJUSTMENT_CONTENTION",
        "Too many concurrent changes to this product's demand; please retry.",
    )


def _adjust_surplus_attempt(
    db: Session,
    business: Business,
    surplus_id: uuid.UUID,
    payload: SurplusAdjustmentRequest,
    *,
    business_today: date,
) -> SurplusInventory:
    """Corrected sequence (Independent Implementation Review Remediation Plan v1.2
    §4): discover candidate Orders -> lock the complete sorted Order set -> lock
    Product -> lock Recipe/Ingredient and every Surplus lot for the Product,
    deterministic order -> verify the candidate set is still complete -> read the
    authoritative allocation/protection state under lock -> enforce the
    active-run hard floor (§3) -> decide within-slack vs. reconciliation only now
    (closing Finding 7's TOCTOU — the earlier unlocked "is this within slack" read
    could race a concurrent FEFO allocation that never bumps `SurplusInventory`'s
    own version) -> mutate/recalculate atomically where needed, using targeted
    READY reconciliation scoped to only this lot (§2) -> reevaluate readiness ->
    compare before/after allocation consequences across ALL active allocations per
    affected OrderLine, not only READY-linked ones (§4's acknowledgment guard) ->
    require a matching acknowledgment when required -> verify allocation <=
    physical for every locked lot -> commit once."""
    lot = get_owned_or_404(db, SurplusInventory, surplus_id, business)
    check_version(lot, payload.version, resource="surplus_inventory")

    new_physical_quantity = lot.physical_quantity + payload.quantity_delta
    if new_physical_quantity < 0:
        raise ApiError(
            422,
            "SURPLUS_ADJUSTMENT_INSUFFICIENT_BALANCE",
            "This adjustment would make the Surplus balance negative, which is not permitted.",
        )

    product, locked_orders, locked_lots = _lock_full_graph_for_surplus_mutation(
        db, business, lot.product_id
    )

    # Active-run hard floor (§3) — checked under the now-complete lock, before any
    # mutation, before the acknowledgment machinery is even entered. Distinct from
    # the READY-demotion acknowledgment gate below: `IN_PRODUCTION`-protected
    # demand is never reconciled by this action at all, regardless of
    # acknowledgment — only finishing or canceling the active run can change it.
    protected_requirement_ids = get_protected_requirement_ids(db, business, product)
    active_run_fixed = _active_run_fixed_quantity_for_lot(
        db, business, lot.id, protected_requirement_ids
    )
    if new_physical_quantity < active_run_fixed:
        raise ApiError(
            409,
            "SURPLUS_ADJUSTMENT_PRODUCTION_LOCKED",
            "This lot has Surplus committed to an active production run and cannot be "
            "reduced below that commitment. Finish or cancel the active production run "
            "first.",
        )

    active_allocated = _active_allocated_quantity(db, business, lot.id)

    if new_physical_quantity >= active_allocated:
        # Within unallocated slack — decided only now, under the complete lock
        # graph, so the race Finding 7 identified cannot occur: any concurrent
        # Order confirmation/edit/cancel that could add a new SurplusAllocation
        # against this Product must first acquire the same Product lock this
        # function already holds.
        lot.physical_quantity = _persist_quantity(new_physical_quantity, field="physical_quantity")
        db.add(
            SurplusTransaction(
                id=uuid.uuid4(),
                business_id=business.id,
                surplus_inventory_id=lot.id,
                transaction_type=SurplusTransactionType.MANUAL_ADJUSTMENT,
                quantity_change=payload.quantity_delta,
                reason=payload.reason.value,
                notes=payload.notes,
            )
        )
        commit_or_raise_stale(db, resource="surplus_inventory")
        return lot

    # Below the currently-allocated quantity — consequential reconciliation.
    before_by_line = _active_allocation_by_line_for_orders(db, business, locked_orders.keys())

    lot.physical_quantity = _persist_quantity(new_physical_quantity, field="physical_quantity")
    db.add(
        SurplusTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            surplus_inventory_id=lot.id,
            transaction_type=SurplusTransactionType.MANUAL_ADJUSTMENT,
            quantity_change=payload.quantity_delta,
            reason=payload.reason.value,
            notes=payload.notes,
        )
    )
    db.flush()  # the reduced physical_quantity must be visible to the reallocation below

    # Reuse the existing, already-tested deterministic FEFO allocator — never a new
    # manual release-priority algorithm. Targeted to just this lot (§2): every
    # OTHER lot's READY-linked allocations remain fixed regardless of how many
    # other lots this Product has.
    recalculate_product_closure(
        db,
        business,
        product,
        business_today=business_today,
        ready_surplus_policy=reconcile_ready_for_lots({lot.id}),
    )

    demoted_order_ids: list[uuid.UUID] = []
    for order_id, order in locked_orders.items():
        was_ready = order.status is OrderStatus.READY
        readiness_service.evaluate_and_apply_readiness(db, business, order)
        if was_ready and order.status is not OrderStatus.READY:
            demoted_order_ids.append(order_id)

    after_by_line = _active_allocation_by_line_for_orders(db, business, locked_orders.keys())
    reduced_line_ids = sorted(
        (
            line_id
            for line_id, before_qty in before_by_line.items()
            if after_by_line.get(line_id, Decimal(0)) < before_qty
        ),
        key=str,
    )

    if demoted_order_ids or reduced_line_ids:
        fingerprint = _surplus_reduction_fingerprint(
            lot.id,
            new_physical_quantity=lot.physical_quantity,
            demoted_order_ids=demoted_order_ids,
            reduced_line_ids=reduced_line_ids,
        )
        if payload.acknowledged_warning_fingerprint != fingerprint:
            db.rollback()
            raise ApiError(
                409,
                "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT",
                "This adjustment will reduce currently-promised Surplus allocations. "
                "Review and acknowledge before proceeding.",
                issues=[
                    {
                        "severity": "WARNING",
                        "code": "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION",
                        "message": (
                            f"{len(reduced_line_ids)} order line(s) will lose some or all of "
                            f"their promised Surplus allocation, and {len(demoted_order_ids)} "
                            "Ready order(s) will be demoted to Confirmed."
                        ),
                        "field": None,
                        "resource": "surplus_inventory",
                        "details": {
                            "demoted_order_ids": [str(i) for i in demoted_order_ids],
                            "reduced_order_line_ids": [str(i) for i in reduced_line_ids],
                            "fingerprint": fingerprint,
                        },
                    }
                ],
            )

    # All-lot defensive invariant (§4/Finding 8): the allocator's own output is
    # constrained by each lot's `physical_quantity` as an input, so this should be
    # unreachable for every lot this recalculation touched — treated as an
    # internal error, never silently committed, checked for every locked lot, not
    # only the one manually adjusted.
    for locked_lot in locked_lots:
        remaining_active = _active_allocated_quantity(db, business, locked_lot.id)
        if remaining_active > locked_lot.physical_quantity:
            db.rollback()
            raise ApiError(
                500,
                "SURPLUS_RECONCILIATION_INVARIANT_VIOLATED",
                "Surplus reconciliation could not establish a valid final state.",
            )

    commit_or_raise_stale(db, resource="surplus_inventory")
    return lot


def list_surplus_for_business(
    db: Session, business: Business, *, limit: int = 50, offset: int = 0
) -> tuple[list[SurplusInventory], int]:
    conditions = [SurplusInventory.business_id == business.id]
    total = db.scalar(select(func.count()).select_from(SurplusInventory).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(SurplusInventory)
            .where(*conditions)
            .order_by(SurplusInventory.produced_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def list_surplus_transactions(
    db: Session, business: Business, surplus_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> tuple[list[SurplusTransaction], int]:
    get_owned_or_404(db, SurplusInventory, surplus_id, business)
    conditions = [
        SurplusTransaction.surplus_inventory_id == surplus_id,
        SurplusTransaction.business_id == business.id,
    ]
    total = db.scalar(select(func.count()).select_from(SurplusTransaction).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(SurplusTransaction)
            .where(*conditions)
            .order_by(SurplusTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total
