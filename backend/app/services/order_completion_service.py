"""Order Completion / Fulfillment: `READY -> COMPLETED` (Spec §4.2/§4.4, §7.15/§7.16,
§11.6/§11.8, PUR-006, AC-COMP-001..005; ADR-034/051/052/057/058/059; Order Completion
slice).

One atomic transaction that physically fulfills the Order and freezes its historical
direct-cost truth:

* deducts Purchased inventory (WAC snapshot) and writes `ORDER_FULFILLMENT` history;
* consumes exactly the Order's active Surplus allocation rows and writes `ORDER_FULFILLMENT`
  Surplus history;
* writes the immutable `PURCHASED_GOOD` / `SURPLUS` / `PACKAGING` / `CUSTOM_DIRECT` ledger
  rows (`PRODUCTION` rows already exist from Finish and are never touched);
* sets `COMPLETED` / `completed_at`, records `READY -> COMPLETED` history, recalculates every
  affected Product closure (releasing the Order's reservations/links), and re-evaluates
  sibling readiness — then commits once.

Locking follows the frozen composed hierarchy exactly as Cancel does (ADR-124/141):
globally id-sorted candidate Orders -> Products -> Recipes/Ingredients -> Surplus lots /
Purchased inventory, with discover-lock-verify and a bounded retry. `ProductionRun` rows are
never locked here (Finish/Cancel lock the run first and then follow this same order, so no
reverse cycle exists). **Active-run protection is re-checked authoritatively after the
Product locks are held**: Start Production locks Product/Requirement but not the Order, so an
early check alone could miss a run created between that check and the Product lock.

A rejected or failed attempt rolls back everything (zero domain mutation). Completion never
commits a demotion on failure (a stale/uncovered READY Order is rejected with
`409 ORDER_COVERAGE_LOST`; the supply-changing workflows own normal READY demotion).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.api_errors import ApiError
from app.core.tenant import (
    NotFoundError,
    check_version,
    commit_or_raise_stale,
    stale_version_error,
)
from app.db.enums import (
    CostType,
    OrderLineType,
    OrderStatus,
    ProductType,
    PurchasedInventoryTransactionType,
    SurplusTransactionType,
)
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.product import Product
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.surplus import (
    PurchasedProductReservation,
    SurplusAllocation,
    SurplusInventory,
    SurplusTransaction,
)
from app.domain import order_completion as plan_domain
from app.services import order_lifecycle_service, order_service, readiness_service
from app.services.operational_recalculation_service import recalculate_product_closure

_MAX_DISCOVERY_RETRIES = 3

_COMPLETION_COST_TYPES = (
    CostType.PURCHASED_GOOD,
    CostType.SURPLUS,
    CostType.PACKAGING,
    CostType.CUSTOM_DIRECT,
)


class _CandidateOrderSetGrew(Exception):
    """Internal-only retry signal (a distinct instance per module, by convention)."""


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


def complete_order(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    *,
    expected_version: int,
    business_today,
) -> Order:
    """Bounded-retry wrapper around `_complete_order_attempt` (the identical
    discover-lock-verify-retry protocol Confirm/Cancel/Finish use, ADR-141)."""
    for _attempt in range(_MAX_DISCOVERY_RETRIES):
        try:
            return _complete_order_attempt(
                db,
                business,
                order_id,
                expected_version=expected_version,
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


# --------------------------------------------------------------------------------------
# Attempt
# --------------------------------------------------------------------------------------


def _complete_order_attempt(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    *,
    expected_version: int,
    business_today,
) -> Order:
    # 2-4. Non-locking preflight (discovery only), then the entire candidate Order set is
    # locked ONCE, globally id-sorted — the directly-addressed Order is never locked
    # separately or first.
    preflight = order_service.get_order_for_business(db, order_id, business)
    preflight_products = {
        line.product_id for line in preflight.lines if line.product_id is not None
    }
    candidate_ids = order_lifecycle_service._discover_full_order_set(
        db, business, order_id, preflight_products
    )
    locked_orders = order_lifecycle_service._lock_orders_sorted(db, business, candidate_ids)
    order = locked_orders.get(order_id)
    if order is None:
        raise NotFoundError()
    # The preflight above loaded these rows BEFORE the locks were granted, and a
    # `SELECT ... FOR UPDATE` does not overwrite attributes of an instance already in the
    # identity map. Expire everything so the version/status/line checks below read the
    # post-lock, committed truth (a concurrent winner's commit must be visible here).
    db.expire_all()

    try:
        # 5-6. Expected version, then status.
        check_version(order, expected_version, resource="order")
        if order.status is not OrderStatus.READY:
            raise ApiError(
                409,
                "ORDER_NOT_COMPLETABLE",
                "Only a Ready order can be completed.",
                issues=[
                    {
                        "severity": "ERROR",
                        "code": "ORDER_NOT_COMPLETABLE",
                        "message": f"This order is {order.status.value}, not Ready.",
                        "field": None,
                        "resource": "order",
                        "details": {"status": order.status.value},
                    }
                ],
            )
        # Optional fast rejection only — NOT authoritative (see the post-Product-lock
        # re-check below).
        order_lifecycle_service._reject_if_production_locked(db, business, order)

        # 7-8. Products (sorted) -> Recipe/Ingredient -> Surplus lots / Purchased inventory.
        products = order_lifecycle_service._acquire_operational_lock_graph(db, business, order)
        db.expire_all()  # same reason: re-read every fact only after all locks are held

        # 9. Re-discover under Product serialization; bounded retry if the set grew.
        s1 = order_lifecycle_service._discover_full_order_set(
            db, business, order_id, {p.id for p in products}
        )
        if not s1.issubset(locked_orders.keys()):
            raise _CandidateOrderSetGrew()

        # 10. AUTHORITATIVE active-run re-check: Product serialization is now held, so a
        # concurrent Start Production for these Products cannot cross this point.
        order_lifecycle_service._reject_if_production_locked(db, business, order)

        # 11-12. Authoritative coverage revalidation + invariant guard.
        _reject_if_prior_completion_rows_exist(db, business, order)
        plan = _revalidate_coverage_and_build_plan(db, business, order, products)

        # 14-17. Physical mutations and historical ledger (all in this transaction).
        _apply_purchased_fulfillment(db, business, order, plan)
        _apply_surplus_consumption(db, business, order, plan)
        _write_ledger_rows(db, business, order, plan)

        # 18-20. Order lifecycle.
        _finalize_order(db, business, order)

        # 21-22. Release the Order's operational claims; sibling readiness.
        _recalculate_and_reevaluate(
            db, business, order, products, locked_orders, business_today=business_today
        )

        # 23. Post-conditions.
        _assert_post_conditions(db, business, order, products, plan)
    except _CandidateOrderSetGrew:
        raise
    except StaleDataError:
        db.rollback()
        raise stale_version_error(resource="order") from None
    except Exception:
        db.rollback()
        raise

    # 24. Commit once with the standard stale-write translation.
    commit_or_raise_stale(db, resource="order")
    return order


# --------------------------------------------------------------------------------------
# Revalidation + plan
# --------------------------------------------------------------------------------------


def _coverage_lost(failures: list[dict]) -> ApiError:
    return ApiError(
        409,
        "ORDER_COVERAGE_LOST",
        "This order is no longer fully covered and cannot be completed. Review its "
        "fulfillment state; nothing was changed.",
        issues=[
            {
                "severity": "ERROR",
                "code": f["code"],
                "message": f["message"],
                "field": None,
                "resource": "order",
                "details": f["details"],
            }
            for f in failures
        ],
    )


def _reject_if_prior_completion_rows_exist(db: Session, business: Business, order: Order) -> None:
    """Pre-existing Completion-created ledger rows can only mean corrupted state (the
    READY -> COMPLETED transition happens once). Treated as an invariant violation —
    never repaired (ADR-105 convention)."""
    existing = db.scalar(
        select(func.count())
        .select_from(OrderCostAllocation)
        .where(
            OrderCostAllocation.order_id == order.id,
            OrderCostAllocation.business_id == business.id,
            OrderCostAllocation.cost_type.in_(_COMPLETION_COST_TYPES),
        )
    )
    if existing:
        raise ApiError(
            500,
            "ORDER_COMPLETION_INVARIANT_VIOLATED",
            "Order completion could not establish a valid state.",
        )


def _revalidate_coverage_and_build_plan(
    db: Session, business: Business, order: Order, products: list[Product]
) -> plan_domain.CompletionPlan:
    failures: list[dict] = []

    # Authoritative fulfillment coverage under every lock — the existing, frozen
    # derivation (produced historical output + active Surplus; Purchased aggregate
    # shortage; manual Custom Item satisfaction). No second algorithm is introduced.
    if not readiness_service.is_order_fully_satisfied(db, business, order):
        for unsatisfied in readiness_service.unsatisfied_lines(db, business, order):
            failures.append(
                {
                    "code": "ORDER_LINE_NOT_COVERED",
                    "message": f"{unsatisfied.display_name} is no longer fully covered.",
                    "details": {
                        "order_line_id": str(unsatisfied.line_id),
                        "reason": unsatisfied.reason,
                    },
                }
            )
        if not failures:
            failures.append(
                {
                    "code": "ORDER_NOT_FULLY_COVERED",
                    "message": "This order is no longer fully covered.",
                    "details": {},
                }
            )
        raise _coverage_lost(failures)

    product_by_id = {p.id: p for p in products}
    purchased_lines = [
        plan_domain.PurchasedLineFact(line.id, line.product_id, line.underlying_quantity)
        for line in order.lines
        if line.line_type is not OrderLineType.CUSTOM_ITEM
        and line.product_id is not None
        and product_by_id[line.product_id].product_type is ProductType.PURCHASED
    ]

    # The Order's authoritative Purchased reservations must equal its Purchased demand.
    if purchased_lines:
        reserved_by_line = {
            line_id: Decimal(total)
            for line_id, total in db.execute(
                select(
                    PurchasedProductReservation.order_line_id,
                    func.coalesce(func.sum(PurchasedProductReservation.quantity), 0),
                )
                .where(
                    PurchasedProductReservation.order_id == order.id,
                    PurchasedProductReservation.business_id == business.id,
                )
                .group_by(PurchasedProductReservation.order_line_id)
            )
        }
        for line in purchased_lines:
            if reserved_by_line.get(line.line_id, Decimal(0)) != line.quantity:
                failures.append(
                    {
                        "code": "PURCHASED_RESERVATION_MISMATCH",
                        "message": "This order's Purchased reservation does not match its demand.",
                        "details": {
                            "order_line_id": str(line.line_id),
                            "required_quantity": str(line.quantity),
                            "reserved_quantity": str(
                                reserved_by_line.get(line.line_id, Decimal(0))
                            ),
                        },
                    }
                )

    inventory_rows = (
        list(
            db.scalars(
                select(PurchasedProductInventory).where(
                    PurchasedProductInventory.business_id == business.id,
                    PurchasedProductInventory.product_id.in_(
                        {line.product_id for line in purchased_lines}
                    ),
                )
            )
        )
        if purchased_lines
        else []
    )
    purchased_inventory = {
        row.product_id: plan_domain.PurchasedInventoryFact(
            row.product_id, row.physical_quantity, row.weighted_average_unit_cost
        )
        for row in inventory_rows
    }

    allocations = list(
        db.scalars(
            select(SurplusAllocation).where(
                SurplusAllocation.order_id == order.id,
                SurplusAllocation.business_id == business.id,
            )
        )
    )
    lot_ids = {a.surplus_inventory_id for a in allocations}
    lots = (
        list(
            db.scalars(
                select(SurplusInventory).where(
                    SurplusInventory.id.in_(lot_ids),
                    SurplusInventory.business_id == business.id,
                )
            )
        )
        if lot_ids
        else []
    )
    surplus_lots = {
        lot.id: plan_domain.SurplusLotFact(lot.id, lot.physical_quantity, lot.unit_cost_basis)
        for lot in lots
    }

    plan, plan_failures = plan_domain.build_completion_plan(
        purchased_lines=purchased_lines,
        purchased_inventory=purchased_inventory,
        surplus_allocations=[
            plan_domain.SurplusAllocationFact(
                a.id, a.surplus_inventory_id, a.order_line_id, a.quantity
            )
            for a in allocations
        ],
        surplus_lots=surplus_lots,
        line_snapshots=[
            plan_domain.LineSnapshotFact(
                line.id, line.packaging_cost_total_snapshot, line.custom_direct_cost_estimate
            )
            for line in order.lines
        ],
    )
    for failure in plan_failures:
        if failure.code == "COMPLETION_AMOUNT_NOT_REPRESENTABLE":
            raise ApiError(
                422,
                "ORDER_COMPLETION_AMOUNT_NOT_REPRESENTABLE",
                failure.message,
                issues=[
                    {
                        "severity": "ERROR",
                        "code": failure.code,
                        "message": failure.message,
                        "field": None,
                        "resource": "order",
                        "details": failure.details,
                    }
                ],
            )
        failures.append(
            {"code": failure.code, "message": failure.message, "details": failure.details}
        )
    if failures or plan is None:
        raise _coverage_lost(failures)
    return plan


# --------------------------------------------------------------------------------------
# Physical mutations + ledger (each a separately named step so failure injection in tests
# can prove atomic rollback at every boundary)
# --------------------------------------------------------------------------------------


def _apply_purchased_fulfillment(
    db: Session, business: Business, order: Order, plan: plan_domain.CompletionPlan
) -> None:
    if not plan.purchased:
        return
    inventories = {
        row.product_id: row
        for row in db.scalars(
            select(PurchasedProductInventory).where(
                PurchasedProductInventory.business_id == business.id,
                PurchasedProductInventory.product_id.in_(plan.purchased_deduction_by_product),
            )
        )
    }
    for product_id, quantity in plan.purchased_deduction_by_product.items():
        # WAC and all other cost fields are deliberately left untouched.
        inventories[product_id].physical_quantity = (
            inventories[product_id].physical_quantity - quantity
        )
    for line_plan in plan.purchased:
        db.add(
            PurchasedProductInventoryTransaction(
                id=line_plan.transaction_id,
                business_id=business.id,
                product_id=line_plan.product_id,
                transaction_type=PurchasedInventoryTransactionType.ORDER_FULFILLMENT,
                quantity_change=-line_plan.quantity,
                unit_cost=line_plan.unit_cost,
                total_cost=line_plan.total_cost,
                order_id=order.id,
                order_line_id=line_plan.line_id,
            )
        )
    db.flush()


def _apply_surplus_consumption(
    db: Session, business: Business, order: Order, plan: plan_domain.CompletionPlan
) -> None:
    if not plan.surplus:
        return
    lots = {
        lot.id: lot
        for lot in db.scalars(
            select(SurplusInventory).where(
                SurplusInventory.business_id == business.id,
                SurplusInventory.id.in_(plan.surplus_consumption_by_lot),
            )
        )
    }
    for lot_id, quantity in plan.surplus_consumption_by_lot.items():
        lots[lot_id].physical_quantity = lots[lot_id].physical_quantity - quantity
    for consumption in plan.surplus:
        db.add(
            SurplusTransaction(
                id=consumption.transaction_id,
                business_id=business.id,
                surplus_inventory_id=consumption.lot_id,
                transaction_type=SurplusTransactionType.ORDER_FULFILLMENT,
                quantity_change=-consumption.quantity,
                order_id=order.id,
                order_line_id=consumption.line_id,
            )
        )
    db.flush()


def _write_ledger_rows(
    db: Session, business: Business, order: Order, plan: plan_domain.CompletionPlan
) -> None:
    for purchased in plan.purchased:
        db.add(
            OrderCostAllocation(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                order_line_id=purchased.line_id,
                cost_type=CostType.PURCHASED_GOOD,
                amount=purchased.ledger_amount,
                purchased_inventory_transaction_id=purchased.transaction_id,
            )
        )
    for surplus in plan.surplus:
        db.add(
            OrderCostAllocation(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                order_line_id=surplus.line_id,
                cost_type=CostType.SURPLUS,
                amount=surplus.ledger_amount,
                surplus_inventory_id=surplus.lot_id,
            )
        )
    for packaging in plan.packaging:
        db.add(
            OrderCostAllocation(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                order_line_id=packaging.line_id,
                cost_type=CostType.PACKAGING,
                amount=packaging.amount,
            )
        )
    for custom in plan.custom_direct:
        db.add(
            OrderCostAllocation(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                order_line_id=custom.line_id,
                cost_type=CostType.CUSTOM_DIRECT,
                amount=custom.amount,
            )
        )
    # The consumed operational Surplus allocation rows are projection state; their
    # historical record is now the Surplus transaction + SURPLUS ledger row.
    for allocation in db.scalars(
        select(SurplusAllocation).where(
            SurplusAllocation.order_id == order.id,
            SurplusAllocation.business_id == business.id,
        )
    ):
        db.delete(allocation)
    db.flush()


def _finalize_order(db: Session, business: Business, order: Order) -> None:
    now = datetime.now(UTC)
    order.status = OrderStatus.COMPLETED
    order.completed_at = now
    # `ready_at` is retained as lifecycle history (Cancel clears it only because the
    # Order leaves the fulfillment path; a Completed Order was genuinely Ready).
    db.add(
        OrderStatusHistory(
            id=uuid.uuid4(),
            business_id=business.id,
            order_id=order.id,
            from_status=OrderStatus.READY,
            to_status=OrderStatus.COMPLETED,
            changed_at=now,
        )
    )
    order_service.touch_order(order)
    db.flush()


def _recalculate_and_reevaluate(
    db: Session,
    business: Business,
    order: Order,
    products: list[Product],
    locked_orders: dict[uuid.UUID, Order],
    *,
    business_today,
) -> None:
    # The Order's status is already COMPLETED (flushed), so each Product closure is
    # rebuilt without its demand: its Purchased reservations and remaining rebuildable
    # links are released, and siblings' rebuildable allocations are re-fit against the
    # reduced lot physical. Historical ProductionRun / allocation / ledger rows are never
    # read or written by recalculation.
    for product in products:
        recalculate_product_closure(db, business, product, business_today=business_today)
    for other_id, other_order in locked_orders.items():
        if other_id != order.id:
            readiness_service.evaluate_and_apply_readiness(db, business, other_order)
    db.flush()


def _assert_post_conditions(
    db: Session,
    business: Business,
    order: Order,
    products: list[Product],
    plan: plan_domain.CompletionPlan,
) -> None:
    def violated() -> ApiError:
        return ApiError(
            500,
            "ORDER_COMPLETION_INVARIANT_VIOLATED",
            "Order completion could not establish a valid final state.",
        )

    product_ids = [p.id for p in products]
    if product_ids:
        negative_purchased = db.scalar(
            select(func.count())
            .select_from(PurchasedProductInventory)
            .where(
                PurchasedProductInventory.business_id == business.id,
                PurchasedProductInventory.product_id.in_(product_ids),
                PurchasedProductInventory.physical_quantity < 0,
            )
        )
        if negative_purchased:
            raise violated()

        for lot in db.scalars(
            select(SurplusInventory).where(
                SurplusInventory.business_id == business.id,
                SurplusInventory.product_id.in_(product_ids),
            )
        ):
            allocated = db.scalar(
                select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
                    SurplusAllocation.surplus_inventory_id == lot.id,
                    SurplusAllocation.business_id == business.id,
                )
            )
            if Decimal(allocated) > lot.physical_quantity:
                raise violated()

    # Historical allocations equal the plan exactly; no duplicate fulfillment rows.
    rows = list(
        db.scalars(
            select(OrderCostAllocation).where(
                OrderCostAllocation.order_id == order.id,
                OrderCostAllocation.business_id == business.id,
                OrderCostAllocation.cost_type.in_(_COMPLETION_COST_TYPES),
            )
        )
    )
    actual = sorted((r.cost_type.value, str(r.order_line_id), str(r.amount)) for r in rows)
    expected = sorted(
        [
            *[
                (CostType.PURCHASED_GOOD.value, str(p.line_id), str(p.ledger_amount))
                for p in plan.purchased
            ],
            *[(CostType.SURPLUS.value, str(s.line_id), str(s.ledger_amount)) for s in plan.surplus],
            *[(CostType.PACKAGING.value, str(p.line_id), str(p.amount)) for p in plan.packaging],
            *[
                (CostType.CUSTOM_DIRECT.value, str(c.line_id), str(c.amount))
                for c in plan.custom_direct
            ],
        ]
    )
    if actual != expected:
        raise violated()
    purchased_txns = db.scalar(
        select(func.count())
        .select_from(PurchasedProductInventoryTransaction)
        .where(
            PurchasedProductInventoryTransaction.order_id == order.id,
            PurchasedProductInventoryTransaction.business_id == business.id,
            PurchasedProductInventoryTransaction.transaction_type
            == PurchasedInventoryTransactionType.ORDER_FULFILLMENT,
        )
    )
    surplus_txns = db.scalar(
        select(func.count())
        .select_from(SurplusTransaction)
        .where(
            SurplusTransaction.order_id == order.id,
            SurplusTransaction.business_id == business.id,
            SurplusTransaction.transaction_type == SurplusTransactionType.ORDER_FULFILLMENT,
        )
    )
    if purchased_txns != len(plan.purchased) or surplus_txns != len(plan.surplus):
        raise violated()


# --------------------------------------------------------------------------------------
# Read model: per-Order historical direct cost, derived only from immutable ledger rows
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoricalCostAllocation:
    id: uuid.UUID
    cost_type: CostType
    order_line_id: uuid.UUID | None
    amount: Decimal


@dataclass(frozen=True)
class HistoricalCost:
    total: Decimal
    by_type: dict[CostType, Decimal]
    allocations: list[HistoricalCostAllocation]


def get_historical_cost(db: Session, business: Business, order: Order) -> HistoricalCost:
    """The Order's historical direct cost — operational truth for that one Order (not
    Phase 10 Analytics), summed exclusively from its immutable `OrderCostAllocation`
    rows. Read-only; takes no locks."""
    rows = list(
        db.scalars(
            select(OrderCostAllocation)
            .where(
                OrderCostAllocation.order_id == order.id,
                OrderCostAllocation.business_id == business.id,
            )
            .order_by(OrderCostAllocation.cost_type, OrderCostAllocation.id)
        )
    )
    by_type = {cost_type: Decimal("0.00") for cost_type in CostType}
    for row in rows:
        by_type[row.cost_type] += row.amount
    return HistoricalCost(
        total=sum(by_type.values(), Decimal("0.00")),
        by_type=by_type,
        allocations=[
            HistoricalCostAllocation(r.id, r.cost_type, r.order_line_id, r.amount) for r in rows
        ],
    )
