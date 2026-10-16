"""Real two-connection concurrency tests for Order Completion (each thread gets its own real
connection via `real_session_factory`, mirroring the Phase 8 concurrency suites).

Covers: two concurrent Completes on one Order; Completion vs Start Production through the
early-check/Product-lock gap (the authoritative post-Product-lock active-run re-check);
Completion vs Finish / Cancel of a run that protects the Order's demand and of a run that
only concerns sibling demand; Completion vs Purchased adjustment; Completion vs Order
Cancel; and Completion vs a Custom Item manual-fulfillment toggle.
"""

from __future__ import annotations

import threading
from datetime import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.api_errors import ApiError
from app.db.enums import CostType, OrderStatus, ProductionRunStatus
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.production import ProductionRequirement, ProductionRun
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.surplus import SurplusInventory
from app.db.models.user import User
from app.schemas.inventory import ManualAdjustmentReason, PurchasedAdjustmentRequest
from app.services import order_completion_service, order_lifecycle_service
from app.services.order_lifecycle_service import cancel_confirmed_order
from app.services.production_run_service import (
    cancel_production_run,
    finish_production,
    start_production,
)
from app.services.purchased_inventory_service import adjust_purchased_product
from tests.integration.api.completion_support import (
    build_ready_order,
    produced_product,
    purchased_product,
    surplus_lot,
)
from tests.integration.api.phase9_support import business_today, make_stocked_ingredient
from tests.integration.schema.factories import make_business_graph


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


def _run_in_thread(real_session_factory, business_id, results, key, fn, barrier=None):
    def target():
        db = real_session_factory()
        try:
            if barrier is not None:
                barrier.wait(timeout=10)
            business = db.get(Business, business_id)
            results[key] = ("ok", fn(db, business))
        except ApiError as exc:
            results[key] = ("api_error", exc.code)
        except Exception as exc:  # noqa: BLE001
            results[key] = ("error", repr(exc))
        finally:
            db.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread


COMPLETE_LOSER_CODES = {"STALE_VERSION", "ORDER_NOT_COMPLETABLE", "ORDER_COVERAGE_LOST"}


def _assert_structured(results, key, allowed_codes):
    """Every participant in a race ends as a success or a STRUCTURED `ApiError` carrying one
    of the expected conflict codes. A raw exception (`kind == "error"`, e.g. SQLAlchemy's
    `StaleDataError`) is never an acceptable outcome for any participant."""
    kind, detail = results[key]
    assert kind != "error", f"{key} leaked a raw exception: {detail} | {results}"
    if kind == "api_error":
        assert detail in allowed_codes, f"{key}: unexpected code {detail} | {results}"


def _complete(order_id, version):
    def call(db, business):
        order_completion_service.complete_order(
            db,
            business,
            order_id,
            expected_version=version,
            business_today=business_today(business),
        )
        return "completed"

    return call


# ====================================================================== same Order


def test_two_concurrent_completes_on_the_same_order_mutate_exactly_once(real_session_factory):
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        juice = purchased_product(setup, business, physical="10", wac="2.00")
        order = build_ready_order(
            setup, business, [{"product": juice, "qty": "4", "packaging": "0.5"}]
        )
        business_id, order_id, version = business.id, order.id, order.version
    finally:
        setup.close()

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory, business_id, results, i, _complete(order_id, version), barrier
        )
        for i in range(2)
    ]
    for t in threads:
        t.join(timeout=30)

    try:
        outcomes = sorted(kind for kind, _ in results.values())
        assert "error" not in outcomes, results
        assert outcomes == ["api_error", "ok"], results
        loser_code = next(v for k, v in results.values() if k == "api_error")
        assert loser_code in {"STALE_VERSION", "ORDER_NOT_COMPLETABLE"}

        verify = real_session_factory()
        try:
            assert verify.get(Order, order_id).status is OrderStatus.COMPLETED
            inventory = verify.scalars(select(PurchasedProductInventory)).one()
            assert inventory.physical_quantity == Decimal("6")  # deducted exactly once
            assert (
                verify.scalar(
                    select(func.count())
                    .select_from(PurchasedProductInventoryTransaction)
                    .where(PurchasedProductInventoryTransaction.order_id == order_id)
                )
                == 1
            )
            counts = {
                row[0]: row[1]
                for row in verify.execute(
                    select(OrderCostAllocation.cost_type, func.count())
                    .where(OrderCostAllocation.order_id == order_id)
                    .group_by(OrderCostAllocation.cost_type)
                )
            }
            assert counts == {CostType.PURCHASED_GOOD: 1, CostType.PACKAGING: 1}
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# =========================================================== Completion vs Start Production


def _x_and_y_world(db):
    """Order X is READY (covered by surplus); Order Y shares the Product and demand date and
    needs production — so ONE requirement links both lines and Start is possible on it."""
    business = make_business_graph(db)
    flour = make_stocked_ingredient(db, business, name="Flour", physical="100000")
    cookie = produced_product(db, business, ingredients=[(flour, Decimal("500"))])
    surplus_lot(db, business, cookie, physical="6", basis="0.50")
    x = build_ready_order(
        db,
        business,
        [{"product": cookie, "qty": "6"}],
        order_number="ORD-X",
        fulfillment_time=time(9, 0),
    )
    y = build_ready_order(
        db,
        business,
        [{"product": cookie, "qty": "12"}],
        order_number="ORD-Y",
        fulfillment_time=time(15, 0),
        expect_status=None,
    )
    assert x.status is OrderStatus.READY and y.status is OrderStatus.CONFIRMED
    requirement = db.scalars(select(ProductionRequirement)).one()
    return business, x, y, requirement


def test_completion_cannot_miss_a_run_started_between_the_early_check_and_the_product_lock(
    real_session_factory, monkeypatch
):
    setup = real_session_factory()
    try:
        business, x, y, requirement = _x_and_y_world(setup)
        business_id, x_id, x_version, requirement_id = business.id, x.id, x.version, requirement.id
    finally:
        setup.close()

    reached, resume = threading.Event(), threading.Event()
    original = order_lifecycle_service._acquire_operational_lock_graph

    def paused_graph(db, biz, order):
        # Reached AFTER the Order locks and the early active-run check, BEFORE any
        # Product lock is taken.
        reached.set()
        resume.wait(timeout=20)
        return original(db, biz, order)

    monkeypatch.setattr(order_lifecycle_service, "_acquire_operational_lock_graph", paused_graph)

    results: dict = {}
    completer = _run_in_thread(
        real_session_factory, business_id, results, "complete", _complete(x_id, x_version)
    )
    try:
        assert reached.wait(timeout=20), "Completion never reached the Product-lock gap"

        # Start Production locks Product/Requirement but NOT the Order, so it proceeds while
        # Completion holds the Order locks — creating the run inside the gap.
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            run = start_production(
                db, biz, requirement_id, batches=None, acknowledged_warning_fingerprint=None
            )
            db.commit()
            run_id = run.id
        finally:
            db.close()
    finally:
        resume.set()
        completer.join(timeout=30)

    try:
        assert results["complete"] == ("api_error", "ORDER_PRODUCTION_LOCKED"), results
        verify = real_session_factory()
        try:
            assert verify.get(Order, x_id).status is OrderStatus.READY  # nothing mutated
            assert verify.get(ProductionRun, run_id).status is ProductionRunStatus.IN_PRODUCTION
            assert (
                verify.scalar(
                    select(func.count())
                    .select_from(OrderCostAllocation)
                    .where(OrderCostAllocation.order_id == x_id)
                )
                == 0
            )
            lot = verify.scalars(select(SurplusInventory)).one()
            assert lot.physical_quantity == Decimal("6")
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


@pytest.mark.parametrize("run_action", ["finish", "cancel"])
def test_completion_vs_finish_or_cancel_of_a_run_protecting_the_orders_demand(
    real_session_factory, run_action
):
    setup = real_session_factory()
    try:
        business, x, y, requirement = _x_and_y_world(setup)
        run = start_production(
            setup, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup.commit()
        business_id, x_id, x_version = business.id, x.id, x.version
        run_id, run_version = run.id, run.version
    finally:
        setup.close()

    def run_workflow(db, biz):
        if run_action == "finish":
            finish_production(
                db,
                biz,
                run_id,
                expected_version=run_version,
                actual_output_quantity=Decimal("12"),
                ingredient_actual_quantities=None,
                business_today=business_today(biz),
            )
        else:
            cancel_production_run(
                db, biz, run_id, expected_version=run_version, business_today=business_today(biz)
            )
        return run_action

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(x_id, x_version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "run", run_workflow, barrier),
    ]
    for t in threads:
        t.join(timeout=60)

    try:
        assert results["run"][0] == "ok", results
        kind, detail = results["complete"]
        assert kind != "error", results
        verify = real_session_factory()
        try:
            order = verify.get(Order, x_id)
            if kind == "ok":
                # Completion serialized AFTER the run released its protection.
                assert order.status is OrderStatus.COMPLETED
                run_row = verify.get(ProductionRun, run_id)
                assert run_row.status is not ProductionRunStatus.IN_PRODUCTION
            else:
                # Completion serialized while the run was still active: rejected, no change.
                assert detail == "ORDER_PRODUCTION_LOCKED", results
                assert order.status is OrderStatus.READY
                assert (
                    verify.scalar(
                        select(func.count())
                        .select_from(OrderCostAllocation)
                        .where(
                            OrderCostAllocation.order_id == x_id,
                            OrderCostAllocation.cost_type != CostType.PRODUCTION,
                        )
                    )
                    == 0
                )
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_completion_proceeds_while_a_run_only_concerns_sibling_demand(real_session_factory):
    """X (READY, date D1) and Y (CONFIRMED, date D2) share a Product but not a requirement;
    the active run protects only Y's requirement — so X is not production-locked, and
    Completion vs that run's Finish both succeed under the shared Order/Product graph."""
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        flour = make_stocked_ingredient(setup, business, name="Flour", physical="100000")
        cookie = produced_product(setup, business, ingredients=[(flour, Decimal("500"))])
        surplus_lot(setup, business, cookie, physical="6", basis="0.50")
        x = build_ready_order(
            setup,
            business,
            [{"product": cookie, "qty": "6"}],
            order_number="ORD-X",
            fulfillment_offset_days=1,
        )
        y = build_ready_order(
            setup,
            business,
            [{"product": cookie, "qty": "12"}],
            order_number="ORD-Y",
            fulfillment_offset_days=3,
            expect_status=None,
        )
        y_requirement = setup.scalars(
            select(ProductionRequirement).where(
                ProductionRequirement.demand_date == y.fulfillment_date
            )
        ).one()
        run = start_production(
            setup, business, y_requirement.id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup.commit()
        business_id, x_id, x_version, y_id = business.id, x.id, x.version, y.id
        run_id, run_version = run.id, run.version
    finally:
        setup.close()

    def finish(db, biz):
        finish_production(
            db,
            biz,
            run_id,
            expected_version=run_version,
            actual_output_quantity=Decimal("12"),
            ingredient_actual_quantities=None,
            business_today=business_today(biz),
        )
        return "finished"

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(x_id, x_version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "finish", finish, barrier),
    ]
    for t in threads:
        t.join(timeout=60)
    try:
        assert results["complete"][0] == "ok", results
        assert results["finish"][0] == "ok", results
        verify = real_session_factory()
        try:
            assert verify.get(Order, x_id).status is OrderStatus.COMPLETED
            assert (
                verify.get(Order, y_id).status is OrderStatus.READY
            )  # covered by the run's output
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# ============================================================ other competing workflows


@pytest.mark.parametrize("round_", range(4))
def test_completion_vs_purchased_adjustment_never_drives_stock_negative(
    real_session_factory, round_
):
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        juice = purchased_product(setup, business, physical="10", wac="1.00")
        order = build_ready_order(setup, business, [{"product": juice, "qty": "4"}])
        inv = setup.scalars(select(PurchasedProductInventory)).one()
        business_id, order_id, version, product_id, inv_version = (
            business.id,
            order.id,
            order.version,
            juice.id,
            inv.version,
        )
    finally:
        setup.close()

    def adjust(db, biz):
        adjust_purchased_product(
            db,
            biz,
            product_id,
            PurchasedAdjustmentRequest(
                version=inv_version,
                quantity_change=Decimal("-8"),
                reason=ManualAdjustmentReason.DAMAGE,
            ),
        )
        return "adjusted"

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(order_id, version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "adjust", adjust, barrier),
    ]
    for t in threads:
        t.join(timeout=60)
    try:
        _assert_structured(results, "complete", COMPLETE_LOSER_CODES)
        _assert_structured(results, "adjust", {"STALE_VERSION"})
        verify = real_session_factory()
        try:
            inventory = verify.scalars(select(PurchasedProductInventory)).one()
            assert inventory.physical_quantity >= 0
            order_row = verify.get(Order, order_id)
            txns = verify.scalar(
                select(func.count())
                .select_from(PurchasedProductInventoryTransaction)
                .where(PurchasedProductInventoryTransaction.order_id == order_id)
            )
            if results["complete"][0] == "ok":
                assert order_row.status is OrderStatus.COMPLETED and txns == 1
            else:
                assert order_row.status is not OrderStatus.COMPLETED and txns == 0
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


@pytest.mark.parametrize("round_", range(4))
def test_completion_vs_order_cancel_exactly_one_wins(real_session_factory, round_):
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        juice = purchased_product(setup, business, physical="10")
        order = build_ready_order(setup, business, [{"product": juice, "qty": "4"}])
        business_id, order_id, version = business.id, order.id, order.version
    finally:
        setup.close()

    def cancel(db, biz):
        cancel_confirmed_order(
            db, biz, order_id, expected_version=version, business_today=business_today(biz)
        )
        return "canceled"

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(order_id, version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "cancel", cancel, barrier),
    ]
    for t in threads:
        t.join(timeout=60)
    try:
        _assert_structured(results, "complete", COMPLETE_LOSER_CODES)
        _assert_structured(results, "cancel", {"STALE_VERSION", "ORDER_NOT_CANCELABLE"})
        winners = [k for k, (kind, _) in results.items() if kind == "ok"]
        assert len(winners) == 1, results
        verify = real_session_factory()
        try:
            status = verify.get(Order, order_id).status
            inventory = verify.scalars(select(PurchasedProductInventory)).one()
            terminal = verify.scalar(
                select(func.count())
                .select_from(OrderStatusHistory)
                .where(
                    OrderStatusHistory.order_id == order_id,
                    OrderStatusHistory.to_status.in_([OrderStatus.COMPLETED, OrderStatus.CANCELED]),
                )
            )
            assert terminal == 1  # exactly one terminal transition recorded
            if results["complete"][0] == "ok":
                assert status is OrderStatus.COMPLETED and inventory.physical_quantity == Decimal(
                    "6"
                )
            else:
                assert status is OrderStatus.CANCELED and inventory.physical_quantity == Decimal(
                    "10"
                )
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


@pytest.mark.parametrize("round_", range(4))
def test_completion_vs_custom_item_manual_toggle(real_session_factory, round_):
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
        # Mark satisfied through the real workflow so the Order is genuinely READY.
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

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(order_id, version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "toggle", untoggle, barrier),
    ]
    for t in threads:
        t.join(timeout=60)
    try:
        _assert_structured(results, "complete", COMPLETE_LOSER_CODES)
        _assert_structured(
            results, "toggle", {"STALE_VERSION", "ORDER_NOT_ELIGIBLE_FOR_MANUAL_FULFILLMENT"}
        )
        verify = real_session_factory()
        try:
            status = verify.get(Order, order_id).status
            if results["complete"][0] == "ok":
                assert status is OrderStatus.COMPLETED
                assert results["toggle"][0] != "ok"  # a Completed order cannot be toggled
            else:
                assert status is OrderStatus.CONFIRMED  # the toggle demoted it first
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# ============================================================== Completion vs Surplus


def _surplus_backed_order(db):
    business = make_business_graph(db)
    cookie = produced_product(db, business)
    surplus_lot(db, business, cookie, physical="6", basis="0.50")
    order = build_ready_order(db, business, [{"product": cookie, "qty": "6"}])
    lot = db.scalars(select(SurplusInventory)).one()
    return business, cookie, order, lot


@pytest.mark.parametrize("round_", range(4))
def test_completion_vs_surplus_reduction_never_overconsumes(real_session_factory, round_):
    from app.schemas.surplus import SurplusAdjustmentRequest
    from app.services.surplus_service import adjust_surplus

    setup = real_session_factory()
    try:
        business, _cookie, order, lot = _surplus_backed_order(setup)
        business_id, order_id, version, lot_id, lot_version = (
            business.id,
            order.id,
            order.version,
            lot.id,
            lot.version,
        )
    finally:
        setup.close()

    def reduce(db, biz):
        adjust_surplus(
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

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(order_id, version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "reduce", reduce, barrier),
    ]
    for t in threads:
        t.join(timeout=60)
    try:
        _assert_structured(results, "complete", COMPLETE_LOSER_CODES)
        _assert_structured(
            results,
            "reduce",
            {"STALE_VERSION", "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT"},
        )
        verify = real_session_factory()
        try:
            lot_row = verify.get(SurplusInventory, lot_id)
            assert lot_row.physical_quantity >= 0
            status = verify.get(Order, order_id).status
            if results["complete"][0] == "ok":
                assert status is OrderStatus.COMPLETED
                assert lot_row.physical_quantity == Decimal("0")  # exactly the 6 promised
            else:
                assert status is not OrderStatus.COMPLETED
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_completion_vs_surplus_creation_both_succeed(real_session_factory):
    from app.schemas.surplus import SurplusManualCreateRequest
    from app.services.surplus_service import create_surplus_manual

    setup = real_session_factory()
    try:
        business, cookie, order, _lot = _surplus_backed_order(setup)
        business_id, order_id, version, product_id = business.id, order.id, order.version, cookie.id
    finally:
        setup.close()

    def create(db, biz):
        create_surplus_manual(
            db,
            biz,
            SurplusManualCreateRequest(
                product_id=product_id, quantity=Decimal("5"), unit_cost_basis=Decimal("0.25")
            ),
            business_today=business_today(biz),
        )
        return "created"

    results: dict = {}
    barrier = threading.Barrier(2)
    threads = [
        _run_in_thread(
            real_session_factory,
            business_id,
            results,
            "complete",
            _complete(order_id, version),
            barrier,
        ),
        _run_in_thread(real_session_factory, business_id, results, "create", create, barrier),
    ]
    for t in threads:
        t.join(timeout=60)
    try:
        assert results["complete"][0] == "ok", results
        assert results["create"][0] == "ok", results
        verify = real_session_factory()
        try:
            lots = verify.scalars(select(SurplusInventory)).all()
            assert sorted(lot.physical_quantity for lot in lots) == [Decimal("0"), Decimal("5")]
            assert verify.get(Order, order_id).status is OrderStatus.COMPLETED
        finally:
            verify.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
