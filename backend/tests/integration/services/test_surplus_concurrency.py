"""Genuine cross-connection concurrency test for two simultaneous confirmations
competing for the same Surplus lot (Phase 7 Final Remediation Correction Plan,
Finding 7's remaining test gaps).

Uses `real_session_factory` (two independent real connections/transactions),
matching the established pattern in `tests/integration/api/test_orders_concurrency.py`
and `tests/integration/services/test_shared_ingredient_concurrency.py`.
"""

from __future__ import annotations

import threading
from datetime import date, time
from decimal import Decimal

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.production import ProductionRequirement
from app.db.models.surplus import SurplusAllocation
from app.db.models.user import User
from app.services.order_lifecycle_service import confirm_order
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


def test_two_simultaneous_confirmations_competing_for_the_same_surplus_lot(
    real_session_factory,
):
    """Two separate Orders for the SAME Product, same fulfillment date (so they
    aggregate into ONE `ProductionRequirement` group), each demanding 6 units.
    One reusable Surplus lot of exactly 6 units exists — individually, either
    Order's own demand could be fully covered by the lot; combined, only half of
    the 12-unit total demand can be. Both confirms are submitted simultaneously
    against two independent real connections; `_acquire_surplus_and_purchased_
    locks` forces one to fully complete (a full recompute-and-replace of the
    Product's entire closure, PLAN-010) before the other's own lock acquisition
    unblocks. Since `recalculate_product_closure` always rebuilds the Product's
    ENTIRE confirmed-world demand (never an incremental per-order patch), the
    confirm that runs SECOND naturally aggregates BOTH Orders' now-real confirmed
    demand together — proving the lot is allocated exactly once against the true
    combined 12-unit demand, never double-counted as though 6 physical units
    could cover 12 units of demand twice over."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(setup_db, business, product)
        revision = make_recipe_revision(setup_db, business, recipe, yield_quantity=Decimal("12"))
        ingredient = make_ingredient(setup_db, business)
        ingredient.physical_quantity = Decimal("100000")
        make_recipe_revision_ingredient(
            setup_db, business, revision, ingredient, quantity=Decimal("500")
        )
        make_surplus_inventory(setup_db, business, product, physical_quantity=Decimal("6"))

        order_a = make_order(setup_db, business, status=OrderStatus.DRAFT)
        order_a.fulfillment_date = _TODAY
        order_a.fulfillment_time = time(9, 0)
        make_order_line(
            setup_db,
            business,
            order_a,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product,
            underlying_quantity=Decimal("6"),
        )

        order_b = make_order(setup_db, business, status=OrderStatus.DRAFT)
        order_b.fulfillment_date = _TODAY
        order_b.fulfillment_time = time(9, 0)
        make_order_line(
            setup_db,
            business,
            order_b,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product,
            underlying_quantity=Decimal("6"),
        )
        setup_db.commit()
        business_id = business.id
        product_id = product.id
        order_a_id, order_a_version = order_a.id, order_a.version
        order_b_id, order_b_version = order_b.id, order_b.version
    finally:
        setup_db.close()

    start_barrier = threading.Barrier(2)
    results: list[tuple[str, str, object]] = []

    def _confirm(order_id, expected_version, label):
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business_row = db.get(Business, business_id)
            confirmed, _recalc = confirm_order(
                db,
                business_row,
                order_id,
                expected_version=expected_version,
                acknowledged_warning_fingerprints=set(),
                business_today=_TODAY,
            )
            results.append((label, "ok", confirmed.id))
        except ApiError as exc:
            db.rollback()
            results.append((label, "api_error", exc))
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=_confirm, args=(order_a_id, order_a_version, "A")),
            threading.Thread(target=_confirm, args=(order_b_id, order_b_version, "B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 2, results
        # Both confirm cleanly — combined demand (12) still fully covered by
        # surplus (6) + production (6), no shortage, no acknowledgement needed.
        assert all(kind == "ok" for _label, kind, _payload in results), results

        verify_db = real_session_factory()
        try:
            requirements = verify_db.scalars(
                select(ProductionRequirement).where(ProductionRequirement.product_id == product_id)
            ).all()
            # Both Orders' demand aggregates into ONE group (same product,
            # revision, demand date) — never two separate, independently
            # surplus-allocated rows.
            assert len(requirements) == 1, requirements
            requirement = requirements[0]
            assert requirement.confirmed_demand_quantity == Decimal("12.000000")
            assert requirement.surplus_allocated_quantity == Decimal("6.000000")
            assert requirement.production_demand_quantity == Decimal("6.000000")

            allocations = verify_db.scalars(
                select(SurplusAllocation).where(
                    SurplusAllocation.production_requirement_id == requirement.id
                )
            ).all()
            total_allocated = sum((a.quantity for a in allocations), Decimal(0))
            assert total_allocated == Decimal("6.000000")  # the lot's own full capacity, once
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
