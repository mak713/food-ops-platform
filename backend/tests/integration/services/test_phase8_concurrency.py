"""Real two-connection concurrency tests for the remaining Phase 8 interleavings
(Phase 8 Final Architecture Lock §11/§17) not already covered by
`test_production_run_concurrency.py`'s Start-vs-Start test: concurrent Finish on the
same run, concurrent Finish sharing an Ingredient, Finish racing a newly-confirmed
earlier-deadline Order, Purchased mutation racing a concurrent confirmation, and
Surplus reconciliation racing a concurrent confirmation.

Each test uses genuine threads against `TEST_DATABASE_URL` (via `real_session_factory`
— each thread gets its own real connection), mirroring the conventions already
established in `test_optimistic_concurrency.py`/`test_inventory_concurrency.py`.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductionRunStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient, InventoryTransaction
from app.db.models.order import Order
from app.db.models.production import (
    ProductionRequirement,
    ProductionRun,
    ProductionRunOrderAllocation,
)
from app.db.models.surplus import SurplusAllocation, SurplusInventory
from app.db.models.user import User
from app.schemas.inventory import ManualAdjustmentReason, PurchasedRestockRequest
from app.schemas.surplus import SurplusAdjustmentRequest, SurplusManualCreateRequest
from app.services import readiness_service
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.production_run_service import (
    cancel_production_run,
    finish_production,
    start_production,
)
from app.services.purchased_inventory_service import restock_purchased_product
from app.services.surplus_service import adjust_surplus, create_surplus_manual
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_purchased_inventory,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
    make_surplus_allocation,
    make_surplus_inventory,
)

_TODAY = date(2026, 6, 1)


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


class _PauseFirstDiscovery:
    """Temporarily replaces `readiness_service.discover_candidate_order_ids` (a
    module-level function every Phase 8 discover-lock-verify-retry caller accesses
    via `readiness_service.<name>` at call time, per `app/services/*.py`'s shared
    `from app.services import readiness_service` import style) so its GLOBAL first
    invocation across the `with` block computes the real result, signals
    `discovered`, then blocks on `resume` before returning — deterministically
    opening the exact discover -> Product-lock gap the protocol exists to close.
    Every subsequent call (the retry's own S0/S1, or S1 of the first attempt)
    proceeds normally, unpaused. Restores the original function on exit."""

    def __init__(self) -> None:
        self.discovered = threading.Event()
        self.resume = threading.Event()
        self._count = 0
        self._original = readiness_service.discover_candidate_order_ids

    def _wrapper(self, db, business, product_id):
        result = self._original(db, business, product_id)
        self._count += 1
        if self._count == 1:
            self.discovered.set()
            self.resume.wait(timeout=5)
        return result

    def __enter__(self) -> _PauseFirstDiscovery:
        readiness_service.discover_candidate_order_ids = self._wrapper
        return self

    def __exit__(self, *exc) -> None:
        readiness_service.discover_candidate_order_ids = self._original


def _produced_business(real_db, *, demand_quantity: Decimal, fulfillment_date: date):
    business = make_business_graph(real_db)
    product = make_product(real_db, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(real_db, business, product)
    revision = make_recipe_revision(real_db, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(real_db, business)
    ingredient.physical_quantity = Decimal("10000")
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    make_recipe_revision_ingredient(
        real_db, business, revision, ingredient, quantity=Decimal("500")
    )
    order = make_order(real_db, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = fulfillment_date
    line = make_order_line(
        real_db,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=demand_quantity,
    )
    real_db.flush()
    recalculate_product_closure(real_db, business, product, business_today=_TODAY)
    real_db.commit()
    return business, product, ingredient, order, line


# --- 1. Concurrent Finish on the same run ------------------------------------------


def test_concurrent_finish_on_same_run_exactly_one_wins(real_session_factory):
    setup_db = real_session_factory()
    try:
        business, product, ingredient, order, line = _produced_business(
            setup_db, demand_quantity=Decimal("6"), fulfillment_date=_TODAY
        )
        requirement_id = setup_db.scalars(
            select(ProductionRequirement.id).where(ProductionRequirement.product_id == product.id)
        ).one()
        run = start_production(
            setup_db, business, requirement_id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup_db.commit()
        business_id = business.id
        run_id = run.id
    finally:
        setup_db.close()

    results: list[tuple[str, object]] = []
    barrier = threading.Barrier(2)

    def _attempt() -> None:
        db = real_session_factory()
        try:
            barrier.wait(timeout=5)
            biz = db.get(Business, business_id)
            finish_production(
                db,
                biz,
                run_id,
                expected_version=1,
                actual_output_quantity=Decimal("6"),
                ingredient_actual_quantities=None,
                business_today=_TODAY,
            )
            results.append(("ok", None))
        except ApiError as exc:
            results.append(("api_error", exc))
        except Exception as exc:  # noqa: BLE001
            results.append(("error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        oks = [r for kind, r in results if kind == "ok"]
        errors = [r for kind, r in results if kind == "error"]
        assert not errors, f"expected no raw exceptions, got {errors}"
        assert len(oks) == 1, f"expected exactly one winner, got {results}"

        verify_db = real_session_factory()
        try:
            allocations = verify_db.scalars(
                select(ProductionRunOrderAllocation).where(
                    ProductionRunOrderAllocation.production_run_id == run_id
                )
            ).all()
            assert len(allocations) == 1
            assert allocations[0].quantity == Decimal("6.000000")
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 2. Concurrent Finish across two runs sharing an Ingredient --------------------


def test_concurrent_finish_sharing_ingredient_no_lost_update(real_session_factory):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business)
        ingredient.physical_quantity = Decimal("10000")
        ingredient.weighted_average_unit_cost = Decimal("0.02")

        run_ids = []
        for _ in range(2):
            product = make_product(setup_db, business, product_type=ProductType.PRODUCED)
            recipe = make_recipe(setup_db, business, product)
            revision = make_recipe_revision(
                setup_db, business, recipe, yield_quantity=Decimal("12")
            )
            make_recipe_revision_ingredient(
                setup_db, business, revision, ingredient, quantity=Decimal("500")
            )
            order = make_order(setup_db, business, status=OrderStatus.CONFIRMED)
            order.fulfillment_date = _TODAY
            make_order_line(
                setup_db,
                business,
                order,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=product,
                underlying_quantity=Decimal("6"),
            )
            setup_db.flush()
            recalculate_product_closure(setup_db, business, product, business_today=_TODAY)
            setup_db.commit()
            requirement_id = setup_db.scalars(
                select(ProductionRequirement.id).where(
                    ProductionRequirement.product_id == product.id
                )
            ).one()
            run = start_production(
                setup_db,
                business,
                requirement_id,
                batches=None,
                acknowledged_warning_fingerprint=None,
            )
            setup_db.commit()
            run_ids.append(run.id)

        business_id = business.id
        ingredient_id = ingredient.id
        physical_before = ingredient.physical_quantity
    finally:
        setup_db.close()

    results: list[tuple[str, object]] = []
    barrier = threading.Barrier(2)

    def _attempt(run_id) -> None:
        db = real_session_factory()
        try:
            barrier.wait(timeout=5)
            biz = db.get(Business, business_id)
            finish_production(
                db,
                biz,
                run_id,
                expected_version=1,
                actual_output_quantity=Decimal("6"),
                ingredient_actual_quantities=None,
                business_today=_TODAY,
            )
            results.append(("ok", None))
        except Exception as exc:  # noqa: BLE001
            results.append(("error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_attempt, args=(rid,)) for rid in run_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        errors = [r for kind, r in results if kind == "error"]
        assert not errors, f"expected no exceptions, got {errors}"

        verify_db = real_session_factory()
        try:
            fresh_ingredient = verify_db.get(Ingredient, ingredient_id)
            # Both runs consumed 500g each -- no lost update under concurrent
            # row-locked decrement.
            assert fresh_ingredient.physical_quantity == physical_before - Decimal("1000")
            txns = verify_db.scalars(
                select(InventoryTransaction).where(
                    InventoryTransaction.ingredient_id == ingredient_id
                )
            ).all()
            assert len(txns) == 2
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 3. Finish racing a newly-confirmed earlier-deadline Order ---------------------


def test_finish_retries_and_prioritizes_newly_confirmed_earlier_deadline_order(
    real_session_factory,
):
    setup_db = real_session_factory()
    try:
        business, product, ingredient, order_a, line_a = _produced_business(
            setup_db, demand_quantity=Decimal("6"), fulfillment_date=_TODAY + timedelta(days=10)
        )
        requirement_id = setup_db.scalars(
            select(ProductionRequirement.id).where(ProductionRequirement.product_id == product.id)
        ).one()
        run = start_production(
            setup_db, business, requirement_id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup_db.commit()
        business_id = business.id
        product_id = product.id
        run_id = run.id
        line_a_id = line_a.id
    finally:
        setup_db.close()

    order_b_id: list = [None]
    line_b_id: list = [None]

    def _confirm_order_b_while_paused() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            prod = db.get(type(product), product_id)
            order_b = make_order(db, biz, status=OrderStatus.CONFIRMED)
            order_b.fulfillment_date = _TODAY + timedelta(days=1)  # earlier than Order A
            line_b = make_order_line(
                db,
                biz,
                order_b,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=prod,
                underlying_quantity=Decimal("6"),
            )
            db.flush()
            recalculate_product_closure(db, biz, prod, business_today=_TODAY)
            db.commit()
            order_b_id[0] = order_b.id
            line_b_id[0] = line_b.id
        finally:
            db.close()

    finish_result: list = [None]
    finish_error: list = [None]

    def _finish() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            result = finish_production(
                db,
                biz,
                run_id,
                expected_version=1,
                actual_output_quantity=Decimal("6"),  # exactly one 6-unit order's worth
                ingredient_actual_quantities=None,
                business_today=_TODAY,
            )
            finish_result[0] = result
        except Exception as exc:  # noqa: BLE001
            finish_error[0] = exc
        finally:
            db.close()

    try:
        with _PauseFirstDiscovery() as pause:
            finish_thread = threading.Thread(target=_finish)
            finish_thread.start()

            assert pause.discovered.wait(timeout=5), "Finish never reached its first discovery"
            _confirm_order_b_while_paused()
            pause.resume.set()

            finish_thread.join(timeout=10)

        assert finish_error[0] is None, f"Finish raised: {finish_error[0]!r}"
        assert order_b_id[0] is not None, "Order B was never confirmed"

        verify_db = real_session_factory()
        try:
            allocations = verify_db.scalars(
                select(ProductionRunOrderAllocation).where(
                    ProductionRunOrderAllocation.production_run_id == run_id
                )
            ).all()
            by_line = {a.order_line_id: a.quantity for a in allocations}
            # Order B (earlier deadline, confirmed mid-race) must have been
            # discovered via the retry and correctly prioritized over Order A
            # (later deadline, discovered first) -- proving the discover-lock-
            # verify-retry protocol actually closes the race, not merely that it
            # doesn't crash.
            assert by_line.get(line_b_id[0]) == Decimal("6.000000"), by_line
            assert line_a_id not in by_line, by_line

            requirement_a = verify_db.scalars(
                select(ProductionRequirement).where(ProductionRequirement.product_id == product_id)
            ).all()
            # Order A's demand remains as active shortfall (RUN-012).
            assert any(r.production_demand_quantity == Decimal("6.000000") for r in requirement_a)
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 4. Purchased restock racing a concurrent confirmation -------------------------


def test_purchased_restock_retries_for_newly_confirmed_order(real_session_factory):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PURCHASED)
        order_a = make_order(setup_db, business, status=OrderStatus.CONFIRMED)
        order_a.fulfillment_date = _TODAY
        make_order_line(
            setup_db,
            business,
            order_a,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product,
            underlying_quantity=Decimal("3"),
        )
        inventory = make_purchased_inventory(setup_db, business, product, physical_quantity=0)
        setup_db.flush()
        recalculate_product_closure(setup_db, business, product, business_today=_TODAY)
        setup_db.commit()
        business_id = business.id
        product_id = product.id
        order_a_id = order_a.id
        inventory_version = inventory.version
    finally:
        setup_db.close()

    order_b_id: list = [None]

    def _confirm_order_b_while_paused() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            prod = db.get(type(product), product_id)
            order_b = make_order(db, biz, status=OrderStatus.CONFIRMED)
            order_b.fulfillment_date = _TODAY
            make_order_line(
                db,
                biz,
                order_b,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=prod,
                underlying_quantity=Decimal("5"),
            )
            db.flush()
            recalculate_product_closure(db, biz, prod, business_today=_TODAY)
            db.commit()
            order_b_id[0] = order_b.id
        finally:
            db.close()

    restock_error: list = [None]

    def _restock() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            restock_purchased_product(
                db,
                biz,
                product_id,
                PurchasedRestockRequest(
                    quantity=Decimal("10"), unit_cost=Decimal("1.00"), version=inventory_version
                ),
            )
        except Exception as exc:  # noqa: BLE001
            restock_error[0] = exc
        finally:
            db.close()

    try:
        with _PauseFirstDiscovery() as pause:
            restock_thread = threading.Thread(target=_restock)
            restock_thread.start()

            assert pause.discovered.wait(timeout=5), "Restock never reached its first discovery"
            _confirm_order_b_while_paused()
            pause.resume.set()

            restock_thread.join(timeout=10)

        assert restock_error[0] is None, f"Restock raised: {restock_error[0]!r}"
        assert order_b_id[0] is not None

        verify_db = real_session_factory()
        try:
            from app.db.models.order import Order

            fresh_a = verify_db.get(Order, order_a_id)
            fresh_b = verify_db.get(Order, order_b_id[0])
            # 10 units restocked >= 3 (A) + 5 (B) = 8 -- both must have been
            # discovered (via retry) and correctly promoted, not just the one
            # visible at the moment of the restock's initial discovery.
            assert fresh_a.status == OrderStatus.READY, fresh_a.status
            assert fresh_b.status == OrderStatus.READY, fresh_b.status
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 5. Surplus reduction reconciliation racing a concurrent confirmation ----------


def test_surplus_reconciliation_vs_concurrent_confirmation_no_deadlock(real_session_factory):
    setup_db = real_session_factory()
    try:
        business, product, ingredient, order_a, line_a = _produced_business(
            setup_db, demand_quantity=Decimal("4"), fulfillment_date=_TODAY
        )
        lot = make_surplus_inventory(
            setup_db, business, product, physical_quantity=Decimal("10"), is_reusable=True
        )
        setup_db.commit()
        # Recalculate again now that the surplus lot exists, so it actually
        # allocates against Order A's demand.
        recalculate_product_closure(setup_db, business, product, business_today=_TODAY)
        setup_db.commit()
        # Promote Order A to READY -- otherwise there is nothing to "demote" and
        # the reconciliation legitimately requires no acknowledgment at all.
        readiness_service.evaluate_and_apply_readiness(setup_db, business, order_a)
        setup_db.commit()
        assert order_a.status == OrderStatus.READY

        business_id = business.id
        product_id = product.id
        lot_id = lot.id
        lot_version = lot.version
    finally:
        setup_db.close()

    order_b_id: list = [None]

    def _confirm_order_b_while_paused() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            prod = db.get(type(product), product_id)
            order_b = make_order(db, biz, status=OrderStatus.CONFIRMED)
            order_b.fulfillment_date = _TODAY + timedelta(days=1)
            make_order_line(
                db,
                biz,
                order_b,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=prod,
                underlying_quantity=Decimal("2"),
            )
            db.flush()
            recalculate_product_closure(db, biz, prod, business_today=_TODAY)
            db.commit()
            order_b_id[0] = order_b.id
        finally:
            db.close()

    adjust_error: list = [None]
    adjust_api_error: list = [None]

    def _reduce_surplus() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            adjust_surplus(
                db,
                biz,
                lot_id,
                SurplusAdjustmentRequest(
                    version=lot_version,
                    quantity_delta=Decimal("-8"),  # 10 -> 2, below Order A's allocated 4
                    reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
                ),
                business_today=_TODAY,
            )
        except ApiError as exc:
            adjust_api_error[0] = exc
        except Exception as exc:  # noqa: BLE001
            adjust_error[0] = exc
        finally:
            db.close()

    try:
        with _PauseFirstDiscovery() as pause:
            adjust_thread = threading.Thread(target=_reduce_surplus)
            adjust_thread.start()

            assert pause.discovered.wait(timeout=5), "Adjustment never reached its first discovery"
            _confirm_order_b_while_paused()
            pause.resume.set()

            adjust_thread.join(timeout=10)

        assert adjust_error[0] is None, f"Adjustment raised: {adjust_error[0]!r}"
        # A reduction below the allocated quantity is expected to require
        # acknowledgment (it demotes Order A) -- confirms the transaction reached a
        # clean, well-defined outcome rather than deadlocking or crashing.
        assert adjust_api_error[0] is not None
        assert (
            adjust_api_error[0].code
            == "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT"
        )

        verify_db = real_session_factory()
        try:
            fresh_lot = verify_db.get(SurplusInventory, lot_id)
            # Rejected attempt -- untouched.
            assert fresh_lot.physical_quantity == Decimal("10.000000")
            active_allocated = sum(
                (
                    a.quantity
                    for a in verify_db.scalars(
                        select(SurplusAllocation).where(
                            SurplusAllocation.surplus_inventory_id == lot_id
                        )
                    )
                ),
                Decimal(0),
            )
            # Core invariant never violated even mid-race.
            assert active_allocated <= fresh_lot.physical_quantity
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 6. Cancel Production Run vs Finish Production on the same run (Remediation
# Plan v1.2 §1, pair 1) --------------------------------------------------------------


def test_cancel_vs_finish_same_run_exactly_one_wins(real_session_factory):
    setup_db = real_session_factory()
    try:
        business, product, ingredient, order, line = _produced_business(
            setup_db, demand_quantity=Decimal("6"), fulfillment_date=_TODAY
        )
        requirement_id = setup_db.scalars(
            select(ProductionRequirement.id).where(ProductionRequirement.product_id == product.id)
        ).one()
        run = start_production(
            setup_db, business, requirement_id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup_db.commit()
        business_id = business.id
        run_id = run.id
    finally:
        setup_db.close()

    results: list[tuple[str, str, object]] = []
    barrier = threading.Barrier(2)

    def _cancel() -> None:
        db = real_session_factory()
        try:
            barrier.wait(timeout=5)
            biz = db.get(Business, business_id)
            cancel_production_run(db, biz, run_id, expected_version=1, business_today=_TODAY)
            results.append(("cancel", "ok", None))
        except ApiError as exc:
            results.append(("cancel", "api_error", exc))
        except Exception as exc:  # noqa: BLE001
            results.append(("cancel", "error", exc))
        finally:
            db.close()

    def _finish() -> None:
        db = real_session_factory()
        try:
            barrier.wait(timeout=5)
            biz = db.get(Business, business_id)
            finish_production(
                db,
                biz,
                run_id,
                expected_version=1,
                actual_output_quantity=Decimal("6"),
                ingredient_actual_quantities=None,
                business_today=_TODAY,
            )
            results.append(("finish", "ok", None))
        except ApiError as exc:
            results.append(("finish", "api_error", exc))
        except Exception as exc:  # noqa: BLE001
            results.append(("finish", "error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_cancel), threading.Thread(target=_finish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        errors = [r for _, kind, r in results if kind == "error"]
        assert not errors, f"expected no raw exceptions (e.g. a deadlock timeout), got {errors}"
        oks = [name for name, kind, _ in results if kind == "ok"]
        api_errors = [(name, r) for name, kind, r in results if kind == "api_error"]
        # Exactly one winner (single-resource contest on the ProductionRun row);
        # the loser gets a clean, well-defined rejection, never a deadlock.
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one clean rejection, got {results}"
        assert api_errors[0][1].code in (
            "PRODUCTION_RUN_NOT_IN_PRODUCTION",
            "STALE_VERSION",
        ), api_errors[0][1].code

        verify_db = real_session_factory()
        try:
            fresh_run = verify_db.get(ProductionRun, run_id)
            assert fresh_run.status in (ProductionRunStatus.COMPLETED, ProductionRunStatus.CANCELED)
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 7. Cancel Production Run releasing a fixed Surplus claim, racing a concurrent
# confirmation (Remediation Plan v1.2 §1's exact scenario) --------------------------


def test_cancel_releases_fixed_surplus_racing_concurrent_confirmation(real_session_factory):
    setup_db = real_session_factory()
    try:
        business, product, ingredient, order_a, line_a = _produced_business(
            setup_db, demand_quantity=Decimal("6"), fulfillment_date=_TODAY
        )
        requirement_id = setup_db.scalars(
            select(ProductionRequirement.id).where(ProductionRequirement.product_id == product.id)
        ).one()
        run = start_production(
            setup_db, business, requirement_id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup_db.commit()

        requirement = setup_db.get(ProductionRequirement, requirement_id)
        lot = make_surplus_inventory(setup_db, business, product, physical_quantity=Decimal("6"))
        setup_db.flush()
        make_surplus_allocation(
            setup_db, business, lot, requirement, order_a, line_a, quantity=Decimal("6")
        )
        setup_db.commit()

        business_id = business.id
        product_id = product.id
        run_id = run.id
    finally:
        setup_db.close()

    order_b_id: list = [None]

    def _confirm_order_b_while_paused() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            prod = db.get(type(product), product_id)
            order_b = make_order(db, biz, status=OrderStatus.CONFIRMED)
            order_b.fulfillment_date = _TODAY - timedelta(days=1)  # earlier than Order A
            make_order_line(
                db,
                biz,
                order_b,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=prod,
                underlying_quantity=Decimal("6"),
            )
            db.flush()
            recalculate_product_closure(db, biz, prod, business_today=_TODAY)
            db.commit()
            order_b_id[0] = order_b.id
        finally:
            db.close()

    cancel_error: list = [None]

    def _cancel() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            cancel_production_run(db, biz, run_id, expected_version=1, business_today=_TODAY)
        except Exception as exc:  # noqa: BLE001
            cancel_error[0] = exc
        finally:
            db.close()

    try:
        with _PauseFirstDiscovery() as pause:
            cancel_thread = threading.Thread(target=_cancel)
            cancel_thread.start()

            assert pause.discovered.wait(timeout=5), "Cancel never reached its first discovery"
            _confirm_order_b_while_paused()
            pause.resume.set()

            cancel_thread.join(timeout=10)

        assert cancel_error[0] is None, f"Cancel raised: {cancel_error[0]!r}"
        assert order_b_id[0] is not None, "Order B was never confirmed"

        verify_db = real_session_factory()
        try:
            from app.db.models.order import Order as _Order

            fresh_b = verify_db.get(_Order, order_b_id[0])
            # B (earlier deadline, confirmed mid-race) must have been discovered
            # via the retry and correctly claimed the freed lot -- proving the
            # protocol actually closes this race, not merely that it doesn't crash.
            assert fresh_b.status == OrderStatus.READY, fresh_b.status
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 8. Purchased restock: empty candidate set racing a concurrent confirmation
# (Finding 6) -------------------------------------------------------------------


def test_purchased_restock_empty_candidate_set_races_concurrent_confirmation(
    real_session_factory,
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PURCHASED)
        inventory = make_purchased_inventory(setup_db, business, product, physical_quantity=0)
        setup_db.commit()
        # No candidate Orders at all yet -- S0 is genuinely empty.
        business_id = business.id
        product_id = product.id
        inventory_version = inventory.version
    finally:
        setup_db.close()

    order_id: list = [None]

    def _confirm_order_while_paused() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            prod = db.get(type(product), product_id)
            order = make_order(db, biz, status=OrderStatus.CONFIRMED)
            order.fulfillment_date = _TODAY
            make_order_line(
                db,
                biz,
                order,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=prod,
                underlying_quantity=Decimal("5"),
            )
            db.flush()
            recalculate_product_closure(db, biz, prod, business_today=_TODAY)
            db.commit()
            order_id[0] = order.id
        finally:
            db.close()

    restock_error: list = [None]

    def _restock() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            restock_purchased_product(
                db,
                biz,
                product_id,
                PurchasedRestockRequest(
                    quantity=Decimal("10"), unit_cost=Decimal("1.00"), version=inventory_version
                ),
            )
        except Exception as exc:  # noqa: BLE001
            restock_error[0] = exc
        finally:
            db.close()

    try:
        with _PauseFirstDiscovery() as pause:
            restock_thread = threading.Thread(target=_restock)
            restock_thread.start()

            assert pause.discovered.wait(timeout=5), "Restock never reached its first discovery"
            _confirm_order_while_paused()
            pause.resume.set()

            restock_thread.join(timeout=10)

        assert restock_error[0] is None, f"Restock raised: {restock_error[0]!r}"
        assert order_id[0] is not None

        verify_db = real_session_factory()
        try:
            fresh = verify_db.get(Order, order_id[0])
            # The order confirmed AFTER the restock's initial (empty) discovery
            # must still have been observed via the mandatory S1 re-verification
            # under the Product lock and correctly promoted -- proving the
            # empty-S0 shortcut no longer silently skips this.
            assert fresh.status == OrderStatus.READY, fresh.status
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- 9. Manual Surplus creation: empty candidate set racing a concurrent
# confirmation (Finding 6) -------------------------------------------------------


def test_manual_surplus_creation_empty_candidate_set_races_concurrent_confirmation(
    real_session_factory,
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(setup_db, business, product)
        make_recipe_revision(setup_db, business, recipe, yield_quantity=Decimal("12"))
        setup_db.commit()
        # No candidate Orders at all yet -- S0 is genuinely empty.
        business_id = business.id
        product_id = product.id
    finally:
        setup_db.close()

    order_id: list = [None]

    def _confirm_order_while_paused() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            prod = db.get(type(product), product_id)
            order = make_order(db, biz, status=OrderStatus.CONFIRMED)
            order.fulfillment_date = _TODAY
            make_order_line(
                db,
                biz,
                order,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=prod,
                underlying_quantity=Decimal("5"),
            )
            db.flush()
            recalculate_product_closure(db, biz, prod, business_today=_TODAY)
            db.commit()
            order_id[0] = order.id
        finally:
            db.close()

    create_error: list = [None]

    def _create_surplus() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            create_surplus_manual(
                db,
                biz,
                SurplusManualCreateRequest(
                    product_id=product_id,
                    quantity=Decimal("5"),
                    unit_cost_basis=Decimal("1.00"),
                    is_reusable=True,
                ),
                business_today=_TODAY,
            )
        except Exception as exc:  # noqa: BLE001
            create_error[0] = exc
        finally:
            db.close()

    try:
        with _PauseFirstDiscovery() as pause:
            create_thread = threading.Thread(target=_create_surplus)
            create_thread.start()

            assert pause.discovered.wait(timeout=5), "Creation never reached its first discovery"
            _confirm_order_while_paused()
            pause.resume.set()

            create_thread.join(timeout=10)

        assert create_error[0] is None, f"Manual creation raised: {create_error[0]!r}"
        assert order_id[0] is not None

        verify_db = real_session_factory()
        try:
            fresh = verify_db.get(Order, order_id[0])
            assert fresh.status == OrderStatus.READY, fresh.status
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
