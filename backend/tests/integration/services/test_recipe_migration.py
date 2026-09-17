"""Integration tests for the Recipe Revision impact-choice migration protocol
(Phase 7 Plan v2 §9, corrected by the Final Pre-Implementation Amendment §5/§7/§8, and
by the Phase 7 Final Remediation Correction Plan, Finding 4)."""

from __future__ import annotations

import threading
from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import CalculationStatus, OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.product import Product
from app.db.models.production import ProductionRequirement
from app.db.models.recipe import Recipe, RecipeRevision
from app.db.models.user import User
from app.schemas.recipe import RecipeCreateRequest, RecipeRevisionCreateRequest
from app.services import ingredient_service
from app.services.order_lifecycle_service import confirm_order
from app.services.recipe_service import (
    create_recipe_revision_with_impact,
    create_recipe_with_impact,
)
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_production_run,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)

_TODAY = date(2026, 6, 1)


def _confirm_a_produced_order(
    session: Session, business, product, *, quantity=Decimal("6"), acknowledge_missing_recipe=False
):
    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=quantity,
    )
    session.flush()
    session.commit()
    acknowledged = {f"MISSING_RECIPE:{product.id}"} if acknowledge_missing_recipe else set()
    confirmed, _results = confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=acknowledged,
        business_today=_TODAY,
    )
    session.commit()
    return confirmed


def test_replacement_revision_with_no_affected_demand_needs_no_choice(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    new_revision = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
        ),
        business_today=_TODAY,
    )

    assert new_revision.is_current is True
    assert new_revision.revision_number == 2


def test_replacement_revision_with_affected_demand_requires_choice(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    _confirm_a_produced_order(session, business, product)

    raised = None
    try:
        create_recipe_revision_with_impact(
            session,
            business,
            product.id,
            RecipeRevisionCreateRequest(
                expected_current_revision_id=revision.id,
                yield_quantity=Decimal("12"),
                active_time_minutes=30,
                ingredients=[
                    {"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}
                ],
            ),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "RECIPE_REVISION_IMPACT_REQUIRED"

    # Zero mutation: no second revision was created.
    session.expire_all()
    revisions = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).all()
    assert len(revisions) == 1
    assert revisions[0].recipe_revision_id == revision.id


def test_future_only_leaves_existing_confirmed_demand_on_old_revision(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    old_revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, old_revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    _confirm_a_produced_order(session, business, product)

    new_revision = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=old_revision.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == old_revision.id
    assert requirement.recipe_revision_id != new_revision.id


def test_apply_existing_migrates_confirmed_demand_to_new_revision(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    old_revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, old_revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    _confirm_a_produced_order(session, business, product)

    new_revision = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=old_revision.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
            apply_scope="apply_existing",
        ),
        business_today=_TODAY,
    )
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == new_revision.id


def test_first_recipe_creation_with_affected_incomplete_demand_requires_choice(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    session.commit()

    _confirm_a_produced_order(session, business, product, acknowledge_missing_recipe=True)

    raised = None
    try:
        create_recipe_with_impact(
            session,
            business,
            product.id,
            RecipeCreateRequest(
                name="First Recipe",
                yield_quantity=Decimal("12"),
                active_time_minutes=30,
                ingredients=[
                    {"ingredient_id": ingredient.id, "quantity": Decimal("500"), "unit": "g"}
                ],
            ),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "RECIPE_REVISION_IMPACT_REQUIRED"

    # Zero mutation (Phase 7 Final Remediation Correction Plan, Finding 7): not
    # merely the 422 code, but genuinely zero rows created anywhere.
    session.expire_all()
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id is None  # still INCOMPLETE_RECIPE, untouched
    assert session.scalars(select(Recipe).where(Recipe.product_id == product.id)).first() is None
    assert (
        session.scalars(
            select(RecipeRevision)
            .join(Recipe, RecipeRevision.recipe_id == Recipe.id)
            .where(Recipe.product_id == product.id)
        ).first()
        is None
    )


def test_first_recipe_creation_apply_existing_migrates_confirmed_demand(session: Session):
    """First-Recipe `apply_existing` full completion (Phase 7 Final Remediation
    Correction Plan, Finding 7) — today only the "requires choice" 422 path was
    ever asserted for first-Recipe; this proves the actual migration completes:
    existing INCOMPLETE_RECIPE confirmed demand is re-linked to the brand-new
    revision and recalculated."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    session.commit()

    _confirm_a_produced_order(session, business, product, acknowledge_missing_recipe=True)

    recipe, revision = create_recipe_with_impact(
        session,
        business,
        product.id,
        RecipeCreateRequest(
            name="First Recipe",
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("500"), "unit": "g"}],
            apply_scope="apply_existing",
        ),
        business_today=_TODAY,
    )
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == revision.id
    assert requirement.calculation_status == CalculationStatus.CALCULATED


def test_first_recipe_creation_future_only_leaves_existing_demand_incomplete(session: Session):
    """First-Recipe `future_only` full completion — existing INCOMPLETE_RECIPE
    confirmed demand must NOT be migrated to the brand-new revision; only new
    demand created after this point would use it."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    session.commit()

    _confirm_a_produced_order(session, business, product, acknowledge_missing_recipe=True)

    recipe, revision = create_recipe_with_impact(
        session,
        business,
        product.id,
        RecipeCreateRequest(
            name="First Recipe",
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("500"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id is None  # left INCOMPLETE_RECIPE
    assert requirement.calculation_status == CalculationStatus.INCOMPLETE_RECIPE
    # The Recipe/Revision were still created — only the existing demand's link
    # to it was intentionally withheld.
    assert revision.recipe_id == recipe.id


# --- Phase 7 Final Remediation Correction Plan, Finding 4: full historical-revision -----
# Ingredient lock coverage, proven under genuine concurrency (real connections, not the
# savepoint-wrapped `session` fixture — a Future-Only/Apply-Existing lock-scope bug is
# only ever actually exploitable under real row-lock contention).


def _cleanup_business(real_session_factory, *, business_id):
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


def test_apply_existing_migration_locks_historically_pinned_revision_ingredient(
    real_session_factory, monkeypatch
):
    """revision-1 (ingredient A) -> confirmed order pinned to revision-1 -> Future-Only
    revision-2 (ingredient B) leaves that confirmed demand pinned to revision-1, not
    revision-2 -> Apply-Existing revision-3 (ingredient C) is then created while
    `current` is revision-2. Under the corrected `apply_existing` semantics (Phase 7
    Final Semantic & Precision Correction Plan, Finding 1), revision-1's historical
    demand IS eligible (regardless of not being pinned to the immediately-current
    revision-2) and DOES migrate to revision-3 — proven below by the final
    assertion. This test's *original* purpose remains intact too: the old code's
    ingredient LOCK scope was `current.ingredients` (revision-2's own set, {B})
    union the submitted set ({C}) — completely missing ingredient A, even though
    a live `ProductionRequirementOrder` link still pins real confirmed demand to
    revision-1 and this same recalculation machinery reads/writes reservations
    against it. `revisions_participating_in_closure` closes this by including any
    revision still referenced by a live link, regardless of whether it happens to
    equal `current.id`. Proven via a genuine concurrent NOWAIT probe on a second real
    connection: while revision-3's creation holds its Ingredient locks open, a NOWAIT
    attempt on the historical-only ingredient A must fail."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(setup_db, business, product)
        revision_1 = make_recipe_revision(setup_db, business, recipe, yield_quantity=Decimal("12"))
        historical_ingredient = make_ingredient(setup_db, business, name="Historical Only")
        historical_ingredient.physical_quantity = Decimal("10000")
        make_recipe_revision_ingredient(
            setup_db, business, revision_1, historical_ingredient, quantity=Decimal("500")
        )
        ingredient_b = make_ingredient(setup_db, business, name="Revision Two Only")
        ingredient_b.physical_quantity = Decimal("10000")
        ingredient_c = make_ingredient(setup_db, business, name="Revision Three Only")
        ingredient_c.physical_quantity = Decimal("10000")
        setup_db.commit()
        business_id = business.id
        product_id = product.id
        revision_1_id = revision_1.id
        historical_ingredient_id = historical_ingredient.id
        ingredient_b_id = ingredient_b.id
        ingredient_c_id = ingredient_c.id
    finally:
        setup_db.close()

    confirm_db = real_session_factory()
    try:
        business_row = confirm_db.get(Business, business_id)
        product_row = confirm_db.get(Product, product_id)
        order = make_order(confirm_db, business_row, status=OrderStatus.DRAFT)
        order.fulfillment_date = _TODAY
        order.fulfillment_time = time(9, 0)
        make_order_line(
            confirm_db,
            business_row,
            order,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product_row,
            underlying_quantity=Decimal("6"),
        )
        confirm_db.flush()
        confirm_db.commit()
        confirm_order(
            confirm_db,
            business_row,
            order.id,
            expected_version=order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
        confirm_db.commit()
    finally:
        confirm_db.close()

    revision_2_db = real_session_factory()
    try:
        business_row = revision_2_db.get(Business, business_id)
        revision_2 = create_recipe_revision_with_impact(
            revision_2_db,
            business_row,
            product_id,
            RecipeRevisionCreateRequest(
                expected_current_revision_id=revision_1_id,
                yield_quantity=Decimal("12"),
                active_time_minutes=30,
                ingredients=[
                    {"ingredient_id": ingredient_b_id, "quantity": Decimal("300"), "unit": "g"}
                ],
                apply_scope="future_only",
            ),
            business_today=_TODAY,
        )
        revision_2_id = revision_2.id
        revision_2_db.commit()
    finally:
        revision_2_db.close()

    verify_pin_db = real_session_factory()
    try:
        requirement = verify_pin_db.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product_id)
        ).one()
        assert requirement.recipe_revision_id == revision_1_id  # Future-Only left it here
    finally:
        verify_pin_db.close()

    winner_confirmed_event = threading.Event()
    release_gate = threading.Event()
    real_lock_fn = ingredient_service.lock_ingredients_for_business

    def wrapper(db, ingredient_ids, business_arg):
        result = real_lock_fn(db, ingredient_ids, business_arg)
        winner_confirmed_event.set()
        release_gate.wait(timeout=5)
        return result

    monkeypatch.setattr(ingredient_service, "lock_ingredients_for_business", wrapper)

    migration_results: list[tuple[str, object]] = []

    def _migrate() -> None:
        db = real_session_factory()
        try:
            business_row = db.get(Business, business_id)
            new_revision = create_recipe_revision_with_impact(
                db,
                business_row,
                product_id,
                RecipeRevisionCreateRequest(
                    expected_current_revision_id=revision_2_id,
                    yield_quantity=Decimal("12"),
                    active_time_minutes=30,
                    ingredients=[
                        {"ingredient_id": ingredient_c_id, "quantity": Decimal("300"), "unit": "g"}
                    ],
                    apply_scope="apply_existing",
                ),
                business_today=_TODAY,
            )
            migration_results.append(("ok", new_revision.id))
        except ApiError as exc:
            db.rollback()
            migration_results.append(("error", exc))
        finally:
            db.close()

    thread = threading.Thread(target=_migrate)
    thread.start()
    try:
        assert winner_confirmed_event.wait(timeout=5), "migration never reached the Ingredient lock"

        probe_db = real_session_factory()
        try:
            with pytest.raises(OperationalError):
                probe_db.scalar(
                    select(Ingredient)
                    .where(Ingredient.id == historical_ingredient_id)
                    .with_for_update(nowait=True)
                )
        finally:
            probe_db.rollback()
            probe_db.close()
    finally:
        release_gate.set()
        thread.join(timeout=10)

    assert len(migration_results) == 1 and migration_results[0][0] == "ok", migration_results
    revision_3_id = migration_results[0][1]

    final_db = real_session_factory()
    try:
        requirement = final_db.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product_id)
        ).one()
        # Corrected `apply_existing` semantics (Finding 1): revision-1's historical
        # demand is eligible regardless of not being pinned to the immediately-
        # current revision-2, so it migrates to the newly-created revision-3.
        assert requirement.recipe_revision_id == revision_3_id
        assert requirement.recipe_revision_id != revision_1_id
    finally:
        final_db.close()
        _cleanup_business(real_session_factory, business_id=business_id)


# --- Phase 7 Final Remediation Correction Plan, Findings 4 & 7: migration-vs-order ------
# interleaving (Product-lock-first ordering serializes correctly).


def test_recipe_migration_and_order_confirm_interleave_to_a_consistent_final_state(
    real_session_factory,
):
    """A Recipe-migration replacement revision and a brand-new Order confirmation
    for the SAME Product are submitted simultaneously against two independent real
    connections. Both lock the Product row first (Final Architecture Lock §B:
    `create_recipe_revision_with_impact` via `get_product_for_business_locked`;
    `confirm_order` via `_acquire_operational_lock_graph`'s own Product lock) —
    whichever transaction wins that row lock fully commits before the other's own
    lock acquisition unblocks. Regardless of which one wins:
    - migration-first: the confirm (running second) resolves the Recipe's NEW
      current revision directly, since nothing was linked to the Recipe yet when
      the migration ran — no impact choice was ever needed for the migration.
    - confirm-first: the confirm's own demand is now linked to the OLD revision
      by the time the migration (running second) checks
      `_find_affected_eligible_order_lines` — it correctly detects this as
      affected and (this test submits `apply_scope="apply_existing"`
      unconditionally, valid whether or not there turns out to be anything to
      migrate) migrates it to the new revision.
    Both interleavings converge on the IDENTICAL final state — the order's
    confirmed demand pinned to the NEW revision — proving the lock ordering holds
    under genuine concurrency rather than allowing a torn, order-dependent
    result."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(setup_db, business, product)
        old_revision = make_recipe_revision(
            setup_db, business, recipe, yield_quantity=Decimal("12")
        )
        ingredient = make_ingredient(setup_db, business)
        ingredient.physical_quantity = Decimal("100000")
        make_recipe_revision_ingredient(
            setup_db, business, old_revision, ingredient, quantity=Decimal("500")
        )

        order = make_order(setup_db, business, status=OrderStatus.DRAFT)
        order.fulfillment_date = _TODAY
        order.fulfillment_time = time(9, 0)
        make_order_line(
            setup_db,
            business,
            order,
            line_type=OrderLineType.CUSTOM_QUANTITY,
            product=product,
            underlying_quantity=Decimal("6"),
        )
        setup_db.commit()
        business_id = business.id
        product_id = product.id
        old_revision_id = old_revision.id
        ingredient_id = ingredient.id
        order_id, order_version = order.id, order.version
    finally:
        setup_db.close()

    start_barrier = threading.Barrier(2)
    results: list[tuple[str, str, object]] = []

    def _migrate():
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business_row = db.get(Business, business_id)
            new_revision = create_recipe_revision_with_impact(
                db,
                business_row,
                product_id,
                RecipeRevisionCreateRequest(
                    expected_current_revision_id=old_revision_id,
                    yield_quantity=Decimal("12"),
                    active_time_minutes=30,
                    ingredients=[
                        {"ingredient_id": ingredient_id, "quantity": Decimal("400"), "unit": "g"}
                    ],
                    apply_scope="apply_existing",
                ),
                business_today=_TODAY,
            )
            results.append(("migrate", "ok", new_revision.id))
        except ApiError as exc:
            db.rollback()
            results.append(("migrate", "api_error", exc))
        finally:
            db.close()

    def _confirm():
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business_row = db.get(Business, business_id)
            confirmed, _recalc = confirm_order(
                db,
                business_row,
                order_id,
                expected_version=order_version,
                acknowledged_warning_fingerprints=set(),
                business_today=_TODAY,
            )
            results.append(("confirm", "ok", confirmed.id))
        except ApiError as exc:
            db.rollback()
            results.append(("confirm", "api_error", exc))
        finally:
            db.close()

    try:
        threads = [threading.Thread(target=_migrate), threading.Thread(target=_confirm)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 2, results
        assert all(kind == "ok" for _label, kind, _payload in results), results
        new_revision_id = next(payload for label, _kind, payload in results if label == "migrate")

        verify_db = real_session_factory()
        try:
            requirement = verify_db.scalars(
                select(ProductionRequirement).where(ProductionRequirement.product_id == product_id)
            ).one()
            # Converges to the same final state regardless of interleaving order.
            assert requirement.recipe_revision_id == new_revision_id
            assert requirement.recipe_revision_id != old_revision_id
        finally:
            verify_db.close()
    finally:
        _cleanup_business(real_session_factory, business_id=business_id)


# --- Phase 7 Final Semantic & Precision Correction Plan, Finding 1: apply_existing -----
# migrates ALL eligible confirmed unstarted demand for the Product — current, any
# historical revision, or INCOMPLETE_RECIPE — never merely demand pinned to the
# immediately-current revision.


def test_apply_existing_migrates_eligible_historical_revision_demand(session: Session):
    """Plain, non-concurrency version of the Rev1 -> Rev2 future_only -> Rev3
    apply_existing chain: Revision-1's confirmed demand, left behind by
    Future-Only when Revision-2 was created, is eligible and must migrate when
    Revision-3 is later created with apply_existing — even though it was never
    pinned to Revision-2, the immediately-current revision at that time."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision_1 = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision_1, ingredient, quantity=Decimal("500")
    )
    session.commit()

    _confirm_a_produced_order(session, business, product)

    revision_2 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_1.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == revision_1.id  # Future-Only left it here

    revision_3 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_2.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("300"), "unit": "g"}],
            apply_scope="apply_existing",
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.expire_all()
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == revision_3.id


def test_first_recipe_future_only_then_replacement_apply_existing_migrates_incomplete_demand(
    session: Session,
):
    """First Recipe Rev1 future_only leaves existing confirmed demand
    INCOMPLETE_RECIPE -> a LATER replacement Revision-2 (created via
    create_recipe_revision_with_impact) must detect and migrate that
    still-incomplete demand when apply_existing is chosen. Under the old code,
    create_recipe_revision_with_impact never even looked at NULL-revision demand,
    so Revision-2 would have been created with NO impact choice required at all,
    leaving the demand incomplete forever."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    session.commit()

    _confirm_a_produced_order(session, business, product, acknowledge_missing_recipe=True)

    recipe, revision_1 = create_recipe_with_impact(
        session,
        business,
        product.id,
        RecipeCreateRequest(
            name="First Recipe",
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("500"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id is None  # still INCOMPLETE_RECIPE

    # A replacement revision with NO scope choice must now correctly require one,
    # since the still-incomplete demand is eligible/affected.
    raised = None
    try:
        create_recipe_revision_with_impact(
            session,
            business,
            product.id,
            RecipeRevisionCreateRequest(
                expected_current_revision_id=revision_1.id,
                yield_quantity=Decimal("12"),
                active_time_minutes=30,
                ingredients=[
                    {"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}
                ],
            ),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc
    assert raised is not None
    assert raised.code == "RECIPE_REVISION_IMPACT_REQUIRED"

    session.expire_all()
    revisions = session.scalars(
        select(RecipeRevision).where(RecipeRevision.recipe_id == recipe.id)
    ).all()
    assert len(revisions) == 1  # zero mutation — no second revision created

    revision_2 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_1.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
            apply_scope="apply_existing",
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.expire_all()
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == revision_2.id
    assert requirement.calculation_status == CalculationStatus.CALCULATED


def test_apply_existing_migrates_mixed_historical_current_and_incomplete_demand(
    session: Session,
):
    """Three separate CONFIRMED lines for the same Product — one pinned to a
    historical (non-current) revision, one pinned to the immediately-current
    revision, one INCOMPLETE_RECIPE — a new replacement revision with
    apply_existing must migrate all three together. All three share the same
    demand date, so after migration they merge into ONE ProductionRequirement
    group under the new revision."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    session.commit()

    # C: confirmed before any Recipe exists -> INCOMPLETE_RECIPE, left incomplete
    # forever by First-Recipe future_only.
    _confirm_a_produced_order(
        session, business, product, quantity=Decimal("6"), acknowledge_missing_recipe=True
    )
    recipe, revision_1 = create_recipe_with_impact(
        session,
        business,
        product.id,
        RecipeCreateRequest(
            name="Recipe",
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("500"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()

    # A: confirmed while revision_1 is current -> pinned to revision_1. C's demand
    # is still INCOMPLETE_RECIPE at this point, so every recalculation of this
    # Product (including this confirm) still reports MISSING_RECIPE until C
    # itself migrates — acknowledge it here too, not just for C's own confirm.
    _confirm_a_produced_order(
        session, business, product, quantity=Decimal("6"), acknowledge_missing_recipe=True
    )
    revision_2 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_1.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()

    # B: confirmed while revision_2 is current -> pinned to revision_2. C is
    # still incomplete, so this confirm must also acknowledge MISSING_RECIPE.
    _confirm_a_produced_order(
        session, business, product, quantity=Decimal("6"), acknowledge_missing_recipe=True
    )

    # At this point: C is INCOMPLETE_RECIPE, A is pinned to revision_1 (now
    # historical), B is pinned to revision_2 (now current).
    revision_3 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_2.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("300"), "unit": "g"}],
            apply_scope="apply_existing",
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.expire_all()
    requirements = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).all()
    assert len(requirements) == 1  # A + B + C merged into one group under revision_3
    merged = requirements[0]
    assert merged.recipe_revision_id == revision_3.id
    assert merged.confirmed_demand_quantity == Decimal("18.000000")  # 6 + 6 + 6


def test_apply_existing_leaves_protected_historical_demand_frozen_while_migrating_eligible_demand(
    session: Session,
):
    """A line protected by an active IN_PRODUCTION run stays completely frozen on
    its own historical revision/quantities while a separate, unprotected eligible
    line correctly migrates to the newly-created revision."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision_1 = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision_1, ingredient, quantity=Decimal("500")
    )
    session.commit()

    _confirm_a_produced_order(session, business, product, quantity=Decimal("6"))
    protected_requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    make_production_run(
        session,
        business,
        product,
        revision_1,
        source_production_requirement_id=protected_requirement.id,
    )
    session.commit()
    protected_requirement_id = protected_requirement.id

    # No affected demand at all yet (the only existing demand is protected) — no
    # scope choice required.
    revision_2 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_1.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
        ),
        business_today=_TODAY,
    )
    session.commit()

    # New eligible demand, confirmed while revision_2 is current.
    order_e = make_order(session, business, status=OrderStatus.DRAFT)
    order_e.fulfillment_date = _TODAY
    order_e.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order_e,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    session.commit()
    confirm_order(
        session,
        business,
        order_e.id,
        expected_version=order_e.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    session.commit()

    revision_3 = create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_2.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("300"), "unit": "g"}],
            apply_scope="apply_existing",
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.expire_all()
    still_protected = session.get(ProductionRequirement, protected_requirement_id)
    assert still_protected.recipe_revision_id == revision_1.id  # completely untouched
    assert still_protected.confirmed_demand_quantity == Decimal("6.000000")

    eligible_requirement = session.scalars(
        select(ProductionRequirement).where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.id != protected_requirement_id,
        )
    ).one()
    assert eligible_requirement.recipe_revision_id == revision_3.id


def test_replacement_revision_requires_choice_for_historically_pinned_eligible_demand(
    session: Session,
):
    """Demand pinned to a NON-current historical revision (nothing pinned to the
    immediately-current one) — submitting a replacement revision with NO
    apply_scope must still raise RECIPE_REVISION_IMPACT_REQUIRED, zero mutation.
    Under the old code this eligible-but-historical demand was completely
    invisible to the impact-required check, silently allowing an unguarded
    revision creation with no choice at all."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision_1 = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision_1, ingredient, quantity=Decimal("500")
    )
    session.commit()

    _confirm_a_produced_order(session, business, product)

    create_recipe_revision_with_impact(
        session,
        business,
        product.id,
        RecipeRevisionCreateRequest(
            expected_current_revision_id=revision_1.id,
            yield_quantity=Decimal("12"),
            active_time_minutes=30,
            ingredients=[{"ingredient_id": ingredient.id, "quantity": Decimal("400"), "unit": "g"}],
            apply_scope="future_only",
        ),
        business_today=_TODAY,
    )
    session.commit()
    # Now: demand is pinned to revision_1 (historical); current is revision_2;
    # nothing at all is pinned to revision_2 itself.

    revision_2 = session.scalars(
        select(RecipeRevision).where(
            RecipeRevision.recipe_id == recipe.id, RecipeRevision.is_current.is_(True)
        )
    ).one()

    raised = None
    try:
        create_recipe_revision_with_impact(
            session,
            business,
            product.id,
            RecipeRevisionCreateRequest(
                expected_current_revision_id=revision_2.id,
                yield_quantity=Decimal("12"),
                active_time_minutes=30,
                ingredients=[
                    {"ingredient_id": ingredient.id, "quantity": Decimal("300"), "unit": "g"}
                ],
            ),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc
    assert raised is not None
    assert raised.code == "RECIPE_REVISION_IMPACT_REQUIRED"

    session.expire_all()
    revisions = session.scalars(
        select(RecipeRevision).where(RecipeRevision.recipe_id == recipe.id)
    ).all()
    assert len(revisions) == 2  # zero mutation — no third revision created
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.recipe_revision_id == revision_1.id  # untouched
