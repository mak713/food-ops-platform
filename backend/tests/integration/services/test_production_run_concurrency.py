"""Real two-connection concurrency tests for the Phase 8 Production Run lifecycle
(Phase 8 Final Architecture Lock §11) — genuine thread/connection races against
`TEST_DATABASE_URL`, not simulated within a single session, mirroring the existing
`test_optimistic_concurrency.py`/`test_inventory_concurrency.py` conventions exactly."""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.production import ProductionRequirement, ProductionRun
from app.db.models.user import User
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.production_run_service import start_production
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
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


def test_two_concurrent_starts_for_same_requirement_exactly_one_wins(real_session_factory):
    """Proves the Product-lock-first ordering (Phase 8 Final Architecture Lock §11)
    actually serializes two genuinely concurrent Start attempts against the same
    ProductionRequirement under real Postgres locking — not merely reasoned about —
    and that the partial unique index (`ix_production_runs_unique_active_source`)
    backs it up. Exactly one thread succeeds; the other is cleanly rejected, never a
    raw IntegrityError."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(setup_db, business, product)
        revision = make_recipe_revision(setup_db, business, recipe, yield_quantity=Decimal("12"))
        ingredient = make_ingredient(setup_db, business)
        ingredient.physical_quantity = Decimal("1000")
        ingredient.weighted_average_unit_cost = Decimal("0.02")
        make_recipe_revision_ingredient(setup_db, business, revision, ingredient, quantity=500)
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
        business_id = business.id
        requirement_id = setup_db.scalars(
            select(ProductionRequirement.id).where(ProductionRequirement.product_id == product.id)
        ).one()
    finally:
        setup_db.close()

    results: list[tuple[str, object]] = []
    start_barrier = threading.Barrier(2)

    def _attempt() -> None:
        db = real_session_factory()
        try:
            start_barrier.wait(timeout=5)
            business = db.get(Business, business_id)
            start_production(
                db,
                business,
                requirement_id,
                batches=None,
                acknowledged_warning_fingerprint=None,
            )
            results.append(("ok", None))
        except ApiError as exc:
            results.append(("api_error", exc))
        except Exception as exc:  # noqa: BLE001 - capturing for assertion, not swallowing
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
        api_errors = [r for kind, r in results if kind == "api_error"]
        errors = [r for kind, r in results if kind == "error"]
        assert not errors, f"expected no raw exceptions, got {errors}"
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one clean rejection, got {results}"
        assert api_errors[0].code in ("PRODUCTION_ALREADY_STARTED",), api_errors[0].code

        verify_db = real_session_factory()
        try:
            runs = verify_db.scalars(
                select(ProductionRun).where(
                    ProductionRun.source_production_requirement_id == requirement_id
                )
            ).all()
            assert len(runs) == 1, f"expected exactly one ProductionRun row, found {len(runs)}"
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
