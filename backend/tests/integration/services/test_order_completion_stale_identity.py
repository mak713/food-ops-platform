"""Deterministic stale-identity tests: Order Completion vs the frozen workflows that load their
target row BEFORE taking the Order/Product locks.

Each test makes the competing request (the "loser") load its pre-lock object and then PAUSE
at the lock acquisition; Completion then wins and commits in full; the loser resumes. The
loser's in-memory object is now stale relative to the committed row. The only acceptable
loser outcome is the structured `STALE_VERSION` conflict — never a raw SQLAlchemy
`StaleDataError`, never a partial write.

These tests are not probabilistic: the interleaving is forced with events, not timing. They
fail (raw `StaleDataError`) against the pre-remediation services.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import func, select

from app.core.api_errors import ApiError
from app.db.enums import (
    OrderStatus,
    PurchasedInventoryTransactionType,
    SurplusTransactionType,
)
from app.db.models.business import Business
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.surplus import SurplusAllocation, SurplusInventory, SurplusTransaction
from app.db.models.user import User
from app.schemas.inventory import (
    ManualAdjustmentReason,
    PurchasedAdjustmentRequest,
    PurchasedRestockRequest,
)
from app.schemas.surplus import SurplusAdjustmentRequest
from app.services import (
    order_completion_service,
    order_lifecycle_service,
    purchased_inventory_service,
    surplus_service,
)
from tests.integration.api.completion_support import (
    build_ready_order,
    produced_product,
    purchased_product,
    surplus_lot,
)
from tests.integration.api.phase9_support import business_today
from tests.integration.schema.factories import make_business_graph

LOSER = "stale-loser"


def _cleanup(real_session_factory, *, business_id):
    cleanup = real_session_factory()
    try:
        business = cleanup.get(Business, business_id)
        if business is not None:
            owner_id = business.owner_user_id
            cleanup.delete(business)
            cleanup.flush()
            user = cleanup.get(User, owner_id)
            if user is not None:
                cleanup.delete(user)
        cleanup.commit()
    finally:
        cleanup.close()


def _pause_loser_at(monkeypatch, module, attribute):
    """Wrap `module.attribute` so ONLY the loser thread pauses there (after its pre-lock
    loads, before it acquires any lock). Returns (reached, resume) events."""
    reached, resume = threading.Event(), threading.Event()
    original = getattr(module, attribute)

    def wrapper(*args, **kwargs):
        if threading.current_thread().name == LOSER:
            reached.set()
            assert resume.wait(timeout=30), "loser was never resumed"
        return original(*args, **kwargs)

    monkeypatch.setattr(module, attribute, wrapper)
    return reached, resume


def _race(real_session_factory, monkeypatch, *, business_id, order_id, order_version, hook, loser):
    """Loser loads its pre-lock state and pauses; Completion commits; loser resumes."""
    module, attribute = hook
    reached, resume = _pause_loser_at(monkeypatch, module, attribute)
    outcome: dict = {}

    def target():
        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            outcome["loser"] = ("ok", loser(db, business))
        except ApiError as exc:
            outcome["loser"] = ("api_error", exc.code)
        except Exception as exc:  # noqa: BLE001
            outcome["loser"] = ("error", repr(exc))
        finally:
            db.close()

    thread = threading.Thread(target=target, name=LOSER)
    thread.start()
    try:
        assert reached.wait(timeout=30), "loser never reached the lock-acquisition gap"
        winner = real_session_factory()
        try:
            order_completion_service.complete_order(
                winner,
                winner.get(Business, business_id),
                order_id,
                expected_version=order_version,
                business_today=business_today(winner.get(Business, business_id)),
            )
        finally:
            winner.close()
    finally:
        resume.set()
        thread.join(timeout=60)
    assert not thread.is_alive()
    return outcome["loser"]


def _count(verify, model, *where):
    return verify.scalar(select(func.count()).select_from(model).where(*where))


def _two_purchased_orders(real_session_factory):
    """Stock 10 @ 1.50; X (qty 4) and Z (qty 2) both READY. Z keeps the candidate-Order set
    non-empty after X completes, so the loser takes the explicit-flush readiness path."""
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        juice = purchased_product(setup, business, physical="10", wac="1.50")
        x = build_ready_order(setup, business, [{"product": juice, "qty": "4"}], order_number="X")
        z = build_ready_order(setup, business, [{"product": juice, "qty": "2"}], order_number="Z")
        inventory = setup.scalars(select(PurchasedProductInventory)).one()
        return {
            "business_id": business.id,
            "product_id": juice.id,
            "x_id": x.id,
            "x_version": x.version,
            "z_id": z.id,
            "inventory_version": inventory.version,
        }
    finally:
        setup.close()


def _assert_completion_won_cleanly_purchased(verify, ctx):
    assert verify.get(Order, ctx["x_id"]).status is OrderStatus.COMPLETED
    assert verify.get(Order, ctx["z_id"]).status is OrderStatus.READY
    inventory = verify.scalars(select(PurchasedProductInventory)).one()
    assert inventory.physical_quantity == Decimal("6")  # only Completion's deduction
    assert inventory.weighted_average_unit_cost == Decimal("1.500000")
    by_type = {
        row[0]: row[1]
        for row in verify.execute(
            select(
                PurchasedProductInventoryTransaction.transaction_type,
                func.count(),
            ).group_by(PurchasedProductInventoryTransaction.transaction_type)
        )
    }
    assert by_type.get(PurchasedInventoryTransactionType.ORDER_FULFILLMENT) == 1
    assert PurchasedInventoryTransactionType.MANUAL_ADJUSTMENT not in by_type
    assert PurchasedInventoryTransactionType.RESTOCK not in by_type


def test_purchased_adjustment_that_loaded_before_completion_gets_structured_stale_version(
    real_session_factory, monkeypatch
):
    ctx = _two_purchased_orders(real_session_factory)

    def adjust(db, biz):
        purchased_inventory_service.adjust_purchased_product(
            db,
            biz,
            ctx["product_id"],
            PurchasedAdjustmentRequest(
                version=ctx["inventory_version"],
                quantity_change=Decimal("-3"),
                reason=ManualAdjustmentReason.DAMAGE,
            ),
        )
        return "adjusted"

    try:
        result = _race(
            real_session_factory,
            monkeypatch,
            business_id=ctx["business_id"],
            order_id=ctx["x_id"],
            order_version=ctx["x_version"],
            hook=(purchased_inventory_service, "_lock_candidate_orders_and_product"),
            loser=adjust,
        )
        assert result == ("api_error", "STALE_VERSION"), result
        verify = real_session_factory()
        try:
            _assert_completion_won_cleanly_purchased(verify, ctx)
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=ctx["business_id"])


def test_existing_row_purchased_restock_that_loaded_before_completion_gets_structured_stale_version(
    real_session_factory, monkeypatch
):
    ctx = _two_purchased_orders(real_session_factory)

    def restock(db, biz):
        purchased_inventory_service.restock_purchased_product(
            db,
            biz,
            ctx["product_id"],
            PurchasedRestockRequest(
                version=ctx["inventory_version"], quantity=Decimal("5"), unit_cost=Decimal("2.00")
            ),
        )
        return "restocked"

    try:
        result = _race(
            real_session_factory,
            monkeypatch,
            business_id=ctx["business_id"],
            order_id=ctx["x_id"],
            order_version=ctx["x_version"],
            hook=(purchased_inventory_service, "_lock_candidate_orders_and_product"),
            loser=restock,
        )
        assert result == ("api_error", "STALE_VERSION"), result
        verify = real_session_factory()
        try:
            _assert_completion_won_cleanly_purchased(verify, ctx)
            inventory = verify.scalars(select(PurchasedProductInventory)).one()
            assert inventory.latest_purchase_unit_cost == Decimal("9.990000")  # untouched
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=ctx["business_id"])


def _two_surplus_orders(real_session_factory):
    """One lot of 10 @ 0.50 promised to X (6) and Z (4), both READY."""
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        cookie = produced_product(setup, business)
        surplus_lot(setup, business, cookie, physical="10", basis="0.50")
        x = build_ready_order(setup, business, [{"product": cookie, "qty": "6"}], order_number="X")
        z = build_ready_order(setup, business, [{"product": cookie, "qty": "4"}], order_number="Z")
        lot = setup.scalars(select(SurplusInventory)).one()
        return {
            "business_id": business.id,
            "x_id": x.id,
            "x_version": x.version,
            "z_id": z.id,
            "lot_id": lot.id,
            "lot_version": lot.version,
        }
    finally:
        setup.close()


def test_surplus_reduction_that_loaded_before_completion_gets_structured_stale_version(
    real_session_factory, monkeypatch
):
    """The reduction (-8 of 10) leaves 2, which — against the lot as it was before Completion
    — looks like it merely dips into Z's allocation. After Completion consumed 6, the lot holds
    4, all promised to Z: the stale arithmetic would drive the lot BELOW Z's committed 4 and,
    once reconciliation runs, flush a stale UPDATE (the explicit-flush `StaleDataError` path)."""
    ctx = _two_surplus_orders(real_session_factory)

    def reduce(db, biz):
        surplus_service.adjust_surplus(
            db,
            biz,
            ctx["lot_id"],
            SurplusAdjustmentRequest(
                version=ctx["lot_version"],
                quantity_delta=Decimal("-8"),
                reason=ManualAdjustmentReason.DAMAGE,
            ),
            business_today=business_today(biz),
        )
        return "reduced"

    try:
        result = _race(
            real_session_factory,
            monkeypatch,
            business_id=ctx["business_id"],
            order_id=ctx["x_id"],
            order_version=ctx["x_version"],
            hook=(surplus_service, "_lock_full_graph_for_surplus_mutation"),
            loser=reduce,
        )
        assert result == ("api_error", "STALE_VERSION"), result
        verify = real_session_factory()
        try:
            assert verify.get(Order, ctx["x_id"]).status is OrderStatus.COMPLETED
            assert verify.get(Order, ctx["z_id"]).status is OrderStatus.READY
            lot = verify.scalars(select(SurplusInventory)).one()
            assert lot.physical_quantity == Decimal("4")  # only Completion's consumption
            allocated = verify.scalar(
                select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
                    SurplusAllocation.surplus_inventory_id == lot.id
                )
            )
            assert allocated <= lot.physical_quantity  # allocation <= physical invariant
            assert allocated == Decimal("4")
            kinds = [row[0] for row in verify.execute(select(SurplusTransaction.transaction_type))]
            assert SurplusTransactionType.MANUAL_ADJUSTMENT not in kinds
            assert kinds.count(SurplusTransactionType.ORDER_FULFILLMENT) == 1
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=ctx["business_id"])


def test_simple_surplus_reduction_that_loaded_before_completion_gets_structured_stale_version(
    real_session_factory, monkeypatch
):
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        cookie = produced_product(setup, business)
        surplus_lot(setup, business, cookie, physical="6", basis="0.50")
        x = build_ready_order(setup, business, [{"product": cookie, "qty": "6"}])
        lot = setup.scalars(select(SurplusInventory)).one()
        business_id, x_id, x_version, lot_id, lot_version = (
            business.id,
            x.id,
            x.version,
            lot.id,
            lot.version,
        )
    finally:
        setup.close()

    def reduce(db, biz):
        surplus_service.adjust_surplus(
            db,
            biz,
            lot_id,
            SurplusAdjustmentRequest(
                version=lot_version,
                quantity_delta=Decimal("-4"),
                reason=ManualAdjustmentReason.DAMAGE,
            ),
            business_today=business_today(biz),
        )
        return "reduced"

    try:
        result = _race(
            real_session_factory,
            monkeypatch,
            business_id=business_id,
            order_id=x_id,
            order_version=x_version,
            hook=(surplus_service, "_lock_full_graph_for_surplus_mutation"),
            loser=reduce,
        )
        assert result == ("api_error", "STALE_VERSION"), result
        verify = real_session_factory()
        try:
            assert verify.get(Order, x_id).status is OrderStatus.COMPLETED
            lot_row = verify.scalars(select(SurplusInventory)).one()
            assert lot_row.physical_quantity == Decimal("0")  # exactly the 6 promised
            assert _count(verify, SurplusTransaction) == 1  # the ORDER_FULFILLMENT row only
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_order_cancel_that_loaded_before_completion_gets_structured_stale_version(
    real_session_factory, monkeypatch
):
    ctx = _two_purchased_orders(real_session_factory)

    def cancel(db, biz):
        order_lifecycle_service.cancel_confirmed_order(
            db,
            biz,
            ctx["x_id"],
            expected_version=ctx["x_version"],
            business_today=business_today(biz),
        )
        return "canceled"

    try:
        result = _race(
            real_session_factory,
            monkeypatch,
            business_id=ctx["business_id"],
            order_id=ctx["x_id"],
            order_version=ctx["x_version"],
            hook=(order_lifecycle_service, "_lock_orders_sorted"),
            loser=cancel,
        )
        assert result == ("api_error", "STALE_VERSION"), result
        verify = real_session_factory()
        try:
            _assert_completion_won_cleanly_purchased(verify, ctx)
            assert (
                _count(
                    verify,
                    OrderStatusHistory,
                    OrderStatusHistory.order_id == ctx["x_id"],
                    OrderStatusHistory.to_status == OrderStatus.CANCELED,
                )
                == 0
            )
            assert verify.get(Order, ctx["x_id"]).canceled_at is None
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=ctx["business_id"])


def test_custom_item_toggle_waiting_on_the_order_lock_is_already_structured(
    real_session_factory, monkeypatch
):
    """Inspected-and-found-safe control: `set_custom_item_manual_fulfillment` takes its locked,
    tenant-scoped Order lookup BEFORE any check and loads nothing earlier, so a request paused
    at that lookup while Completion commits reads the committed row and receives a structured
    conflict — it does not share the stale-preflight defect and its code is unchanged."""
    from app.services import order_service

    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        order = build_ready_order(
            setup,
            business,
            [{"product": None, "qty": "1", "manual_required": True}],
            expect_status=None,
        )
        line = order.lines[0]
        order_lifecycle_service.set_custom_item_manual_fulfillment(
            setup, business, order.id, line.id, expected_version=order.version, satisfied=True
        )
        setup.refresh(order)
        assert order.status is OrderStatus.READY
        business_id, order_id, line_id, version = business.id, order.id, line.id, order.version
    finally:
        setup.close()

    def untoggle(db, biz):
        order_lifecycle_service.set_custom_item_manual_fulfillment(
            db, biz, order_id, line_id, expected_version=version, satisfied=False
        )
        return "toggled"

    try:
        result = _race(
            real_session_factory,
            monkeypatch,
            business_id=business_id,
            order_id=order_id,
            order_version=version,
            hook=(order_service, "get_order_for_business_locked"),
            loser=untoggle,
        )
        assert result == ("api_error", "STALE_VERSION"), result
        verify = real_session_factory()
        try:
            assert verify.get(Order, order_id).status is OrderStatus.COMPLETED
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
