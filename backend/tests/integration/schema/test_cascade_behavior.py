"""ON DELETE behavior tests (Phase 1 plan §1.5): one test per rule category, plus
the required full-tenant-graph deletion test and the users<->businesses deletion
protection test.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.schema import factories as f

# The tenant root itself — has no business_id column (it IS the tenant), so it's
# excluded from the discovery query below even though it carries tenant data.
_NON_TENANT_TABLES = {"users", "businesses", "alembic_version"}


def _discover_tenant_tables(session) -> list[str]:
    """Every domain table that carries a business_id column, discovered via
    information_schema rather than hand-maintained — so a future table added
    without updating a hardcoded list can't silently escape this sweep.
    """
    tables = (
        session.execute(
            text(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'business_id'"
            )
        )
        .scalars()
        .all()
    )
    return [t for t in tables if t not in _NON_TENANT_TABLES]


def test_rule_a_business_delete_cascades_to_tenant_row(session):
    business = f.make_business_graph(session)
    session.flush()
    customer = f.make_customer(session, business)
    session.flush()
    customer_id = customer.id

    session.execute(text("DELETE FROM businesses WHERE id = :id"), {"id": business.id})
    session.flush()

    remaining = session.execute(
        text("SELECT COUNT(*) FROM customers WHERE id = :id"), {"id": customer_id}
    ).scalar_one()
    assert remaining == 0


def test_rule_b_master_data_protected_from_isolated_delete(session):
    business = f.make_business_graph(session)
    session.flush()
    used_ingredient = f.make_ingredient(session, business, name="Used Flour")
    unused_ingredient = f.make_ingredient(session, business, name="Unused Sugar")
    session.flush()
    recipe_product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, recipe_product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    f.make_recipe_revision_ingredient(session, business, revision, used_ingredient)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text("DELETE FROM ingredients WHERE id = :id"), {"id": used_ingredient.id}
            )
            session.flush()

    # An unreferenced ingredient deletes cleanly.
    session.execute(text("DELETE FROM ingredients WHERE id = :id"), {"id": unused_ingredient.id})
    session.flush()


def test_rule_c_layered_with_rule_b_product_deletion(session):
    business = f.make_business_graph(session)
    session.flush()

    # An unused product (with an unused selling option and unused recipe) deletes
    # cleanly — Rule C cascades to its own never-referenced children.
    unused_product = f.make_product(session, business)
    session.flush()
    f.make_selling_option(session, business, unused_product)
    f.make_recipe(session, business, unused_product)
    session.flush()
    session.execute(text("DELETE FROM products WHERE id = :id"), {"id": unused_product.id})
    session.flush()

    # A product whose selling option has been used in an order line cannot be
    # deleted — the Rule C cascade toward selling_options hits Rule B protection
    # from order_lines and the whole delete fails.
    used_product = f.make_product(session, business)
    session.flush()
    selling_option = f.make_selling_option(session, business, used_product)
    session.flush()
    order = f.make_order(session, business)
    session.flush()
    f.make_order_line(
        session,
        business,
        order,
        line_type="STANDARD_OPTION",
        product=used_product,
        selling_option=selling_option,
    )
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text("DELETE FROM products WHERE id = :id"), {"id": used_product.id})
            session.flush()


def test_rule_d_provenance_link_set_null_not_blocked(session):
    business = f.make_business_graph(session)
    session.flush()
    product = f.make_product(session, business)
    session.flush()
    recipe = f.make_recipe(session, business, product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    run = f.make_production_run(session, business, product, revision)
    session.flush()
    ingredient = f.make_ingredient(session, business)
    session.flush()
    txn = f.make_inventory_transaction(
        session, business, ingredient, transaction_type="PRODUCTION_CONSUMPTION"
    )
    txn.production_run_id = run.id
    session.flush()
    txn_id = txn.id

    session.execute(text("DELETE FROM production_runs WHERE id = :id"), {"id": run.id})
    session.flush()

    remaining_fk = session.execute(
        text("SELECT production_run_id FROM inventory_transactions WHERE id = :id"),
        {"id": txn_id},
    ).scalar_one()
    assert remaining_fk is None


def test_users_business_deletion_protection(session):
    """A User cannot be deleted while their Business still exists (NO ACTION);
    deleting the Business first, then the User, succeeds."""
    user = f.make_user(session)
    business = f.make_business(session, user)
    session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            session.flush()

    session.execute(text("DELETE FROM businesses WHERE id = :id"), {"id": business.id})
    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    session.flush()


def test_full_tenant_graph_deletion_succeeds(session):
    """Builds a genuinely interconnected tenant graph — including a
    source-linked historical order_cost_allocation (PRODUCTION), not just a
    source-less PACKAGING one — and proves a full `DELETE FROM businesses`
    succeeds cleanly across every dependent table without hitting any of the
    NO ACTION master-data/historical-ledger protections. This is the empirical
    proof for the §1.5 ON DELETE design, not a claim about Postgres internals.
    """
    user = f.make_user(session)
    business = f.make_business(session, user)
    session.flush()

    customer = f.make_customer(session, business)
    ingredient = f.make_ingredient(session, business)
    produced_product = f.make_product(session, business)
    session.flush()

    selling_option = f.make_selling_option(session, business, produced_product)
    recipe = f.make_recipe(session, business, produced_product)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    f.make_recipe_revision_ingredient(session, business, revision, ingredient)
    f.make_inventory_transaction(session, business, ingredient)
    session.flush()

    order = f.make_order(session, business, status="CONFIRMED")
    order.customer_id = customer.id
    session.flush()
    order_line = f.make_order_line(
        session,
        business,
        order,
        line_type="STANDARD_OPTION",
        product=produced_product,
        selling_option=selling_option,
    )
    session.flush()
    f.make_payment(session, business, order)
    f.make_order_status_history(session, business, order)
    session.flush()

    production_requirement = f.make_production_requirement(
        session, business, produced_product, revision
    )
    session.flush()
    f.make_production_requirement_order(
        session, business, production_requirement, order, order_line
    )
    f.make_production_ingredient_requirement(session, business, production_requirement, ingredient)
    f.make_ingredient_reservation(session, business, production_requirement, ingredient)
    session.flush()

    production_run = f.make_production_run(
        session,
        business,
        produced_product,
        revision,
        source_production_requirement_id=production_requirement.id,
    )
    session.flush()
    f.make_production_run_ingredient(session, business, production_run, ingredient)
    run_allocation = f.make_production_run_order_allocation(
        session, business, production_run, order, order_line
    )
    session.flush()

    surplus = f.make_surplus_inventory(
        session, business, produced_product, source_production_run_id=production_run.id
    )
    session.flush()
    f.make_surplus_allocation(session, business, surplus, production_requirement, order, order_line)
    f.make_surplus_transaction(session, business, surplus)
    session.flush()

    purchased_product = f.make_product(session, business, product_type="PURCHASED")
    session.flush()
    f.make_purchased_inventory(session, business, purchased_product)
    purchased_txn = f.make_purchased_inventory_transaction(session, business, purchased_product)
    session.flush()
    f.make_purchased_product_reservation(session, business, purchased_product, order, order_line)
    session.flush()

    # Deliberately exercises a *source-linked* historical cost allocation
    # (PRODUCTION), not just a source-less PACKAGING/CUSTOM_DIRECT one — this is
    # what actually exercises the order_cost_allocations NO ACTION FKs toward
    # production_run_order_allocations/surplus_inventory/purchased_*_transactions.
    f.make_order_cost_allocation(
        session,
        business,
        order,
        cost_type="PRODUCTION",
        production_run_order_allocation_id=run_allocation.id,
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

    business_id = business.id
    user_id = user.id

    tenant_tables = _discover_tenant_tables(session)
    assert len(tenant_tables) == 26, (
        f"expected 26 tenant-owned tables, discovered {len(tenant_tables)}: {tenant_tables}"
    )

    # The actual test: full business deletion must succeed cleanly.
    session.execute(text("DELETE FROM businesses WHERE id = :id"), {"id": business_id})
    session.flush()

    for table in tenant_tables:
        count = session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE business_id = :id"),  # noqa: S608
            {"id": business_id},
        ).scalar_one()
        assert count == 0, f"{table} still has rows scoped to the deleted business"

    # The cascade must stop exactly at the tenant boundary: the owning User is
    # untouched (Business -> User is NO ACTION, not CASCADE).
    remaining_user = session.execute(
        text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    assert remaining_user == 1
