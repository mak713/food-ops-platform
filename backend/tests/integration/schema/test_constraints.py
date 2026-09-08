"""Representative constraint tests: uniqueness, partial indexes, and CHECK
constraints. Not exhaustive over every column (see §4.29 of the Phase 1 plan for
the full numeric-constraint audit) — focused on the spec's special-attention
items plus one representative example of each constraint pattern.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import CalculationStatus
from tests.integration.schema import factories as f


def test_owner_user_id_is_unique(session):
    user = f.make_user(session)
    f.make_business(session, user)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_business(session, user)
            session.flush()


def test_recipe_is_unique_per_business_and_product(session):
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    f.make_recipe(session, business, product)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_recipe(session, business, product)
            session.flush()


def test_at_most_one_current_recipe_revision(session):
    """The partial unique index enforces AT MOST one current revision — not
    "exactly one." "At least one" is a service-layer invariant (Phase 1 plan §1.8),
    not asserted here."""
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, product)
    session.flush()
    f.make_recipe_revision(session, business, recipe, revision_number=1, is_current=True)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_recipe_revision(session, business, recipe, revision_number=2, is_current=True)
            session.flush()

    # A second NON-current revision for the same recipe is unaffected.
    f.make_recipe_revision(session, business, recipe, revision_number=3, is_current=False)
    session.flush()


def test_production_requirements_calculated_uniqueness(session):
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    f.make_production_requirement(session, business, product, revision)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_production_requirement(session, business, product, revision)
            session.flush()


def test_production_requirements_incomplete_recipe_uniqueness(session):
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    f.make_production_requirement(session, business, product, recipe_revision=None)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_production_requirement(session, business, product, recipe_revision=None)
            session.flush()


def test_calculated_and_incomplete_requirements_coexist_for_same_product_date(session):
    """The two partial indexes must not over-constrain: a CALCULATED row and an
    INCOMPLETE_RECIPE row for the same business/product/date are not each other's
    concern (different recipe/product pairing in practice, but proves the indexes
    don't collide)."""
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    import datetime

    today = datetime.date.today()
    f.make_production_requirement(session, business, product, revision, demand_date=today)
    f.make_production_requirement(
        session, business, product, recipe_revision=None, demand_date=today
    )
    session.flush()  # must not raise


def test_production_requirement_revision_status_check(session):
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()

    # Mismatch: has a revision but claims INCOMPLETE_RECIPE.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_production_requirement(
                session,
                business,
                product,
                revision,
                calculation_status=CalculationStatus.INCOMPLETE_RECIPE,
            )
            session.flush()

    # Mismatch: no revision but claims CALCULATED.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_production_requirement(
                session,
                business,
                product,
                recipe_revision=None,
                calculation_status=CalculationStatus.CALCULATED,
            )
            session.flush()


def test_order_cost_allocation_source_exclusivity(session):
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    recipe_product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, recipe_product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    order = f.make_order(session, business)
    order_line = f.make_order_line(session, business, order)
    session.flush()
    production_run = f.make_production_run(session, business, recipe_product, revision)
    session.flush()
    allocation = f.make_production_run_order_allocation(
        session, business, production_run, order, order_line
    )
    surplus = f.make_surplus_inventory(session, business, product)
    session.flush()
    purchased_txn = f.make_purchased_inventory_transaction(session, business, product)
    session.flush()

    # Each of the four valid shapes succeeds.
    f.make_order_cost_allocation(
        session,
        business,
        order,
        cost_type="PRODUCTION",
        production_run_order_allocation_id=allocation.id,
    )
    f.make_order_cost_allocation(
        session, business, order, cost_type="SURPLUS", surplus_inventory_id=surplus.id
    )
    f.make_order_cost_allocation(
        session,
        business,
        order,
        cost_type="PURCHASED_GOOD",
        purchased_inventory_transaction_id=purchased_txn.id,
    )
    f.make_order_cost_allocation(session, business, order, cost_type="PACKAGING")
    session.flush()

    # Invalid: PRODUCTION with a surplus source set too.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_cost_allocation(
                session,
                business,
                order,
                cost_type="PRODUCTION",
                production_run_order_allocation_id=allocation.id,
                surplus_inventory_id=surplus.id,
            )
            session.flush()

    # Invalid: PRODUCTION with no source at all.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_cost_allocation(session, business, order, cost_type="PRODUCTION")
            session.flush()


def test_order_adjustment_requires_description(session):
    business = f.make_business_graph(session)
    session.flush()
    order = f.make_order(session, business)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            order.order_adjustment = 5
            order.adjustment_description = None
            session.flush()


def test_representative_positive_and_non_negative_checks(session):
    business = f.make_business_graph(session)
    session.flush()
    order = f.make_order(session, business)
    session.flush()

    # payments.amount > 0 (strictly positive)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_payment(session, business, order, amount=0)
            session.flush()

    # orders.final_total >= 0 (non-negative, zero allowed)
    order.final_total = 0
    session.flush()  # must not raise
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            order.final_total = -1
            session.flush()


def test_ingredient_physical_quantity_has_no_positivity_check(session):
    """INV-009: real overconsumption may legitimately drive this negative — the
    schema must not block it."""
    business = f.make_business_graph(session)
    session.flush()
    ingredient = f.make_ingredient(session, business, name="Butter")
    session.flush()
    ingredient.physical_quantity = -5
    session.flush()  # must not raise


def test_purchased_product_inventory_physical_quantity_rejects_negative(session):
    """Unlike ingredients.physical_quantity (INV-009 explicitly permits negative),
    purchased_product_inventory has no such carve-out (§8.13: "should not
    intentionally drive... below zero") — the schema enforces `>= 0`."""
    business = f.make_business_graph(session)
    product = f.make_product(session, business, product_type="PURCHASED")
    session.flush()
    inventory = f.make_purchased_inventory(session, business, product)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            inventory.physical_quantity = -1
            session.flush()

    # Zero is still explicitly allowed (>= 0, not > 0).
    inventory.physical_quantity = 0
    session.flush()  # must not raise


def test_orders_status_check_rejects_in_production(session):
    """Regression proof: IN_PRODUCTION was never wired into the persisted Order
    status enum (Spec ORD-002). It's rejected even more fundamentally than by the
    CHECK constraint alone: the column is sized to the longest *valid* status
    value, so 'IN_PRODUCTION' (13 chars) doesn't even fit — Postgres raises a
    DataError (string too long) rather than an IntegrityError (CHECK violation).
    Both are DBAPIError subclasses; either way, the value can never be stored.
    """
    business = f.make_business_graph(session)
    session.flush()

    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO orders (id, business_id, order_number, status) "
                    "VALUES (:id, :business_id, :order_number, 'IN_PRODUCTION')"
                ),
                {"id": uuid.uuid4(), "business_id": business.id, "order_number": "TEST-001"},
            )

    # A same-length-but-invalid value proves the CHECK constraint itself (not
    # just the column's length) rejects unknown status strings.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO orders (id, business_id, order_number, status) "
                    "VALUES (:id, :business_id, :order_number, 'BOGUS')"
                ),
                {"id": uuid.uuid4(), "business_id": business.id, "order_number": "TEST-002"},
            )
