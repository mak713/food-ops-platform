"""Genuine cross-connection concurrency tests for shared-Ingredient contention
across different Products (Phase 7 Final Remediation Correction Plan, Finding 5's
own explicit ask, and part of Finding 7's remaining concurrency-test gaps).

Uses `real_session_factory` (two independent real connections/transactions), never
the savepoint-wrapped `session` fixture — a lock-scope bug here is only ever
actually exploitable under real PostgreSQL row-lock contention, matching the
established pattern in `tests/integration/api/test_orders_concurrency.py` and
`tests/integration/api/test_inventory_concurrency.py`.
"""

from __future__ import annotations

import threading
from datetime import date, time
from decimal import Decimal

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.production import IngredientReservation, ProductionRequirement
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


def test_two_products_confirming_against_a_shared_ingredient_serialize_correctly(
    real_session_factory,
):
    """Two DIFFERENT Products, each with its own separate Order, sharing one
    Ingredient (physical stock 100; each Order's demand is 60 — individually
    fine, jointly a real shortage of 20). Both confirms are submitted
    simultaneously (a `threading.Barrier`, matching `test_orders_concurrency.py`'s
    established style) against two independent real connections. The shared
    Ingredient's row lock (acquired via `_acquire_recipe_and_ingredient_locks` for
    both Products, since both Recipes reference the identical Ingredient id) must
    force one confirm to fully complete — commit or roll back — before the other's
    own lock acquisition unblocks: whichever runs SECOND must see the FIRST's
    already-committed reservation as genuine external demand and correctly detect
    the combined shortage, proving both genuine serialization (no lost update) and
    correct cross-Product combined-shortage arithmetic under real contention."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business)
        ingredient.physical_quantity = Decimal("100")
        setup_db.flush()

        product_a = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe_a = make_recipe(setup_db, business, product_a)
        revision_a = make_recipe_revision(setup_db, business, recipe_a, yield_quantity=Decimal("1"))
        make_recipe_revision_ingredient(
            setup_db, business, revision_a, ingredient, quantity=Decimal("1")
        )

        product_b = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe_b = make_recipe(setup_db, business, product_b)
        revision_b = make_recipe_revision(setup_db, business, recipe_b, yield_quantity=Decimal("1"))
        make_recipe_revision_ingredient(
            setup_db, business, revision_b, ingredient, quantity=Decimal("1")
        )

        order_a = make_order(setup_db, business, status=OrderStatus.DRAFT)
        order_a.fulfillment_date = _TODAY
        order_a.fulfillment_time = time(9, 0)
        make_order_line(
            setup_db,
            business,
            order_a,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product_a,
            underlying_quantity=Decimal("60"),
        )

        order_b = make_order(setup_db, business, status=OrderStatus.DRAFT)
        order_b.fulfillment_date = _TODAY
        order_b.fulfillment_time = time(9, 0)
        make_order_line(
            setup_db,
            business,
            order_b,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product_b,
            underlying_quantity=Decimal("60"),
        )
        setup_db.commit()
        business_id = business.id
        ingredient_id = ingredient.id
        product_a_id, product_b_id = product_a.id, product_b.id
        order_a_id, order_b_id = order_a.id, order_b.id
        order_a_version, order_b_version = order_a.version, order_b.version
    finally:
        setup_db.close()

    start_barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

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
        oks = [r for r in results if r[1] == "ok"]
        errors = [r for r in results if r[1] == "api_error"]
        # Exactly one confirms cleanly (whichever won the Ingredient row lock
        # first genuinely sees zero shortage — baseline 0, its own 60 <= 100);
        # the other, unblocking only after the winner's commit, correctly detects
        # the combined 120 > 100 shortage and is rejected pending acknowledgement.
        assert len(oks) == 1, results
        assert len(errors) == 1, results
        loser_label, _kind, loser_exc = errors[0]
        assert loser_exc.code == "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
        shortage_issues = [i for i in loser_exc.issues if i["code"] == "INGREDIENT_SHORTAGE"]
        assert len(shortage_issues) == 1, shortage_issues
        assert Decimal(shortage_issues[0]["details"]["shortage_quantity"]) == Decimal("20")

        verify_db = real_session_factory()
        try:
            # Exactly one Product's demand was ever actually confirmed/reserved —
            # the loser's rejected attempt left zero mutation of its own.
            requirements = verify_db.scalars(
                select(ProductionRequirement).where(
                    ProductionRequirement.product_id.in_([product_a_id, product_b_id])
                )
            ).all()
            assert len(requirements) == 1, requirements
            reservations = verify_db.scalars(
                select(IngredientReservation).where(
                    IngredientReservation.ingredient_id == ingredient_id
                )
            ).all()
            assert len(reservations) == 1, reservations
            assert reservations[0].quantity_canonical == Decimal("60.000000")
            winner_ingredient = verify_db.get(Ingredient, ingredient_id)
            assert winner_ingredient.physical_quantity == Decimal("100")  # never mutated
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
