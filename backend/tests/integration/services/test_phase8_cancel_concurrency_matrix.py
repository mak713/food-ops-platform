"""Cancel Production Run concurrency matrix (Phase 8 remediation follow-up, item 3).

Genuine independent-connection PostgreSQL tests driving the REAL services (never a
hand-built CONFIRMED Order plus a bare recalculation call) for Cancel Production Run
racing each other operation that contends through the same Product graph:
`confirm_order`, `update_confirmed_order`, `cancel_confirmed_order`, `adjust_surplus`,
Cancel of a different active run, and Finish of a different active run.

The goal is NOT to force a winner. Each pairing is run in two modes:

* `barrier` — both threads released together by a start barrier (natural race);
* `partner_paused` — the mirror image: the partner is paused inside its own
  discover -> lock gap while Cancel runs to completion and commits, then the partner
  resumes and must cope with the changed state (retry, or a domain error);
* `paused` — Cancel is deterministically paused right after its first candidate-Order
  discovery (holding only its own run row), the partner runs to completion and
  commits inside that gap, then Cancel resumes — the exact discover -> lock window
  the retry protocol exists to close. This only exposes the race; it is never the
  correctness mechanism (no sleeps).

Asserted for every pairing: no hung thread, no PostgreSQL deadlock, no raw/
untranslated exception (each outcome is success or an `ApiError` 409/422), and the
committed end state satisfies the allocation/readiness/run-status invariants."""

from __future__ import annotations

import threading
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductionRunStatus, ProductType
from app.db.models.business import Business
from app.db.models.order import Order
from app.db.models.production import (
    ProductionRequirement,
    ProductionRun,
    ProductionRunOrderAllocation,
)
from app.db.models.surplus import SurplusAllocation, SurplusInventory
from app.schemas.inventory import ManualAdjustmentReason
from app.schemas.order import OrderConfirmedEditRequest, OrderLineInput
from app.schemas.surplus import SurplusAdjustmentRequest
from app.services import readiness_service
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.order_lifecycle_service import (
    cancel_confirmed_order,
    confirm_order,
    update_confirmed_order,
)
from app.services.production_run_service import (
    cancel_production_run,
    finish_production,
    start_production,
)
from app.services.surplus_service import adjust_surplus
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
    make_surplus_inventory,
)
from tests.integration.services.test_phase8_concurrency import _cleanup, _PauseFirstDiscovery

_TODAY = date(2026, 6, 1)
_PARTNERS = [
    "confirm_order",
    "update_confirmed_order",
    "cancel_confirmed_order",
    "adjust_surplus",
    "cancel_other_run",
    "finish_other_run",
]


def _order(db, business, product, *, status, days, quantity):
    order = make_order(db, business, status=status)
    order.fulfillment_date = _TODAY + timedelta(days=days)
    order.fulfillment_time = time(9, 0)
    line = make_order_line(
        db,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal(quantity),
    )
    return order, line


def _build_scenario(real_session_factory) -> dict:
    """Product P; lot L (3 units). Order A (day 0, 6) holds L and has active Run A.
    Order X (day 1, 6) has active Run X. Order D (day 2, 4) is CONFIRMED with no run.
    Order E (day 3, 4) is DRAFT."""
    db = real_session_factory()
    try:
        business = make_business_graph(db)
        product = make_product(db, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(db, business, product)
        revision = make_recipe_revision(db, business, recipe, yield_quantity=Decimal("12"))
        ingredient = make_ingredient(db, business)
        ingredient.physical_quantity = Decimal("100000")
        ingredient.weighted_average_unit_cost = Decimal("0.02")
        make_recipe_revision_ingredient(db, business, revision, ingredient, quantity=Decimal("500"))
        lot = make_surplus_inventory(db, business, product, physical_quantity=Decimal("3"))
        order_a, _ = _order(db, business, product, status=OrderStatus.CONFIRMED, days=0, quantity=6)
        order_x, _ = _order(db, business, product, status=OrderStatus.CONFIRMED, days=1, quantity=6)
        order_d, line_d = _order(
            db, business, product, status=OrderStatus.CONFIRMED, days=2, quantity=4
        )
        order_e, _ = _order(db, business, product, status=OrderStatus.DRAFT, days=3, quantity=4)
        db.flush()
        recalculate_product_closure(db, business, product, business_today=_TODAY)
        db.commit()

        req_by_date = {
            r.demand_date: r.id
            for r in db.scalars(
                select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
            )
        }
        run_a = start_production(
            db, business, req_by_date[_TODAY], batches=None, acknowledged_warning_fingerprint=None
        )
        run_x = start_production(
            db,
            business,
            req_by_date[_TODAY + timedelta(days=1)],
            batches=None,
            acknowledged_warning_fingerprint=None,
        )
        db.commit()
        db.refresh(order_d)
        edit_payload = OrderConfirmedEditRequest(
            version=order_d.version,
            customer_id=order_d.customer_id,
            fulfillment_date=order_d.fulfillment_date,
            fulfillment_time=order_d.fulfillment_time,
            fulfillment_method=order_d.fulfillment_method,
            fulfillment_details=order_d.fulfillment_details,
            fulfillment_notes=order_d.fulfillment_notes,
            internal_notes=order_d.internal_notes,
            order_adjustment=order_d.order_adjustment or Decimal("0"),
            adjustment_description=order_d.adjustment_description,
            manual_tax=order_d.manual_tax or Decimal("0"),
            lines=[
                OrderLineInput(
                    id=line_d.id,
                    line_type=OrderLineType.CUSTOM_QUANTITY,
                    product_id=product.id,
                    underlying_quantity=Decimal("5"),
                    charged_unit_price=Decimal("10.00"),
                )
            ],
            acknowledged_warning_fingerprints=[],
        )
        for obj in (order_e, lot, run_a, run_x):
            db.refresh(obj)
        return {
            "business_id": business.id,
            "product_id": product.id,
            "lot_id": lot.id,
            "lot_version": lot.version,
            "order_ids": [order_a.id, order_x.id, order_d.id, order_e.id],
            "order_d_id": order_d.id,
            "order_d_version": order_d.version,
            "order_e_id": order_e.id,
            "order_e_version": order_e.version,
            "run_a_id": run_a.id,
            "run_a_version": run_a.version,
            "run_x_id": run_x.id,
            "run_x_version": run_x.version,
            "edit_payload": edit_payload,
        }
    finally:
        db.close()


def _operation(kind: str, ids: dict):
    """Returns `fn(db, business)` performing the named service call."""

    def cancel_run_a(db, business):
        return cancel_production_run(
            db,
            business,
            ids["run_a_id"],
            expected_version=ids["run_a_version"],
            business_today=_TODAY,
        )

    def confirm_e(db, business):
        return confirm_order(
            db,
            business,
            ids["order_e_id"],
            expected_version=ids["order_e_version"],
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )

    def update_d(db, business):
        return update_confirmed_order(
            db,
            business,
            ids["order_d_id"],
            ids["edit_payload"],
            expected_version=ids["order_d_version"],
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )

    def cancel_d(db, business):
        return cancel_confirmed_order(
            db,
            business,
            ids["order_d_id"],
            expected_version=ids["order_d_version"],
            business_today=_TODAY,
        )

    def adjust_lot(db, business):
        return adjust_surplus(
            db,
            business,
            ids["lot_id"],
            SurplusAdjustmentRequest(
                version=ids["lot_version"],
                quantity_delta=Decimal("-1"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )

    def cancel_x(db, business):
        return cancel_production_run(
            db,
            business,
            ids["run_x_id"],
            expected_version=ids["run_x_version"],
            business_today=_TODAY,
        )

    def finish_x(db, business):
        return finish_production(
            db,
            business,
            ids["run_x_id"],
            expected_version=ids["run_x_version"],
            actual_output_quantity=Decimal("6"),
            ingredient_actual_quantities=None,
            business_today=_TODAY,
        )

    return {
        "cancel_run_a": cancel_run_a,
        "confirm_order": confirm_e,
        "update_confirmed_order": update_d,
        "cancel_confirmed_order": cancel_d,
        "adjust_surplus": adjust_lot,
        "cancel_other_run": cancel_x,
        "finish_other_run": finish_x,
    }[kind]


def _run(real_session_factory, business_id, fn, results, label, barrier=None):
    db = real_session_factory()
    try:
        if barrier is not None:
            barrier.wait(timeout=10)
        business = db.get(Business, business_id)
        fn(db, business)
        results.append((label, "ok", None))
    except ApiError as exc:
        db.rollback()
        results.append((label, "api_error", exc))
    except BaseException as exc:  # noqa: BLE001 - captured for the assertion, not swallowed
        db.rollback()
        results.append((label, "error", exc))
    finally:
        db.close()


def _assert_invariants(real_session_factory, ids: dict, results) -> None:
    raw = [(label, exc) for label, kind, exc in results if kind == "error"]
    assert not raw, f"raw/untranslated exception(s): {raw}"
    for label, kind, exc in results:
        if kind == "api_error":
            assert exc.status_code in (409, 422), (label, exc.code, exc.status_code)
    assert len(results) == 2, results

    db = real_session_factory()
    try:
        business = db.get(Business, ids["business_id"])
        cancel_result = next(r for r in results if r[0] == "cancel_run_a")
        run_a = db.get(ProductionRun, ids["run_a_id"])
        expected = (
            ProductionRunStatus.CANCELED
            if cancel_result[1] == "ok"
            else ProductionRunStatus.IN_PRODUCTION
        )
        assert run_a.status == expected, (run_a.status, results)

        # SUM(active allocation) <= physical, for every lot of the Product.
        for lot in db.scalars(
            select(SurplusInventory).where(SurplusInventory.product_id == ids["product_id"])
        ):
            total = db.scalar(
                select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
                    SurplusAllocation.surplus_inventory_id == lot.id
                )
            )
            assert total <= lot.physical_quantity, (lot.id, total, lot.physical_quantity)

        # No duplicate claims: at most one row per (lot, line).
        duplicates = db.execute(
            select(SurplusAllocation.surplus_inventory_id, SurplusAllocation.order_line_id)
            .group_by(SurplusAllocation.surplus_inventory_id, SurplusAllocation.order_line_id)
            .having(func.count() > 1)
        ).all()
        assert duplicates == []

        # Readiness: every READY order is genuinely satisfied; no CONFIRMED order is
        # satisfied-but-unpromoted (a lost readiness update).
        for order in db.scalars(select(Order).where(Order.id.in_(ids["order_ids"]))):
            satisfied = readiness_service.is_order_fully_satisfied(db, business, order)
            if order.status is OrderStatus.READY:
                assert satisfied, f"READY order {order.id} is not satisfied"
            if order.status is OrderStatus.CONFIRMED:
                assert not satisfied, f"lost readiness update for {order.id}"
            for line in order.lines:
                covered = db.scalar(
                    select(func.coalesce(func.sum(SurplusAllocation.quantity), 0)).where(
                        SurplusAllocation.order_line_id == line.id
                    )
                ) + db.scalar(
                    select(func.coalesce(func.sum(ProductionRunOrderAllocation.quantity), 0)).where(
                        ProductionRunOrderAllocation.order_line_id == line.id
                    )
                )
                assert covered <= line.underlying_quantity, (line.id, covered)

        # An IN_PRODUCTION run must still have its protected requirement.
        for run in db.scalars(
            select(ProductionRun).where(
                ProductionRun.product_id == ids["product_id"],
                ProductionRun.status == ProductionRunStatus.IN_PRODUCTION,
            )
        ):
            assert db.get(ProductionRequirement, run.source_production_requirement_id) is not None
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["barrier", "paused", "partner_paused"])
@pytest.mark.parametrize("partner", _PARTNERS)
def test_cancel_run_vs_partner_no_deadlock_and_invariants_hold(
    real_session_factory, partner: str, mode: str
):
    ids = _build_scenario(real_session_factory)
    business_id = ids["business_id"]
    results: list = []
    cancel_fn = _operation("cancel_run_a", ids)
    partner_fn = _operation(partner, ids)
    try:
        if mode == "barrier":
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(
                    target=_run,
                    args=(real_session_factory, business_id, cancel_fn, results, "cancel_run_a"),
                    kwargs={"barrier": barrier},
                ),
                threading.Thread(
                    target=_run,
                    args=(real_session_factory, business_id, partner_fn, results, partner),
                    kwargs={"barrier": barrier},
                ),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not any(t.is_alive() for t in threads), "hung thread (possible deadlock)"
        elif mode == "partner_paused":
            with _PauseFirstDiscovery() as pause:
                partner_thread = threading.Thread(
                    target=_run,
                    args=(real_session_factory, business_id, partner_fn, results, partner),
                )
                partner_thread.start()
                assert pause.discovered.wait(timeout=10), "partner never reached discovery"
                _run(real_session_factory, business_id, cancel_fn, results, "cancel_run_a")
                pause.resume.set()
                partner_thread.join(timeout=30)
                assert not partner_thread.is_alive(), "hung partner thread (possible deadlock)"
        else:
            with _PauseFirstDiscovery() as pause:
                cancel_thread = threading.Thread(
                    target=_run,
                    args=(real_session_factory, business_id, cancel_fn, results, "cancel_run_a"),
                )
                cancel_thread.start()
                assert pause.discovered.wait(timeout=10), "Cancel never reached discovery"
                _run(real_session_factory, business_id, partner_fn, results, partner)
                pause.resume.set()
                cancel_thread.join(timeout=30)
                assert not cancel_thread.is_alive(), "hung Cancel thread (possible deadlock)"
        _assert_invariants(real_session_factory, ids, results)
    finally:
        _cleanup(real_session_factory, business_id=business_id)
