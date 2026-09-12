"""Direct tests for the scoped-lookup helpers in app.core.tenant (Phase 3 plan v3 §3a/§17).

Lives alongside `factories.py` (this package's own migrate/session fixtures) rather than
`tests/unit/`, since these need real Business/Customer rows against a migrated schema.
Deliberately does NOT inspect emitted SQL (v3 review point 6: dropped as brittle) —
functional indistinguishability between "missing" and "foreign-tenant" is what the spec
actually requires, and the query's own structure is visible during code review of
app/core/tenant.py itself.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.tenant import (
    NotFoundError,
    get_owned_or_404,
    get_recipe_for_product,
    get_recipe_revision_for_recipe,
    get_selling_option_for_product,
    lock_recipe_for_product,
)
from app.db.models.customer import Customer
from app.services.ingredient_service import lock_ingredients_for_business
from app.services.product_service import get_product_for_business_locked
from tests.integration.schema.factories import (
    make_business_graph,
    make_customer,
    make_ingredient,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_selling_option,
)


def test_get_owned_or_404_missing_row_raises_not_found(session):
    business = make_business_graph(session)
    session.flush()

    with pytest.raises(NotFoundError) as excinfo:
        get_owned_or_404(session, Customer, uuid.uuid4(), business)
    assert excinfo.value.code == "NOT_FOUND"


def test_get_owned_or_404_foreign_tenant_row_raises_not_found_identically(session):
    owner_business = make_business_graph(session)
    other_business = make_business_graph(session)
    foreign_customer = make_customer(session, other_business)
    session.flush()

    with pytest.raises(NotFoundError) as missing_excinfo:
        get_owned_or_404(session, Customer, uuid.uuid4(), owner_business)
    with pytest.raises(NotFoundError) as foreign_excinfo:
        get_owned_or_404(session, Customer, foreign_customer.id, owner_business)

    # Same code/message/issues regardless of cause — the two cases are structurally
    # indistinguishable to the caller (Spec §10.5).
    assert missing_excinfo.value.code == foreign_excinfo.value.code == "NOT_FOUND"
    assert missing_excinfo.value.message == foreign_excinfo.value.message
    assert missing_excinfo.value.issues == foreign_excinfo.value.issues == []


def test_get_owned_or_404_owned_row_is_returned(session):
    business = make_business_graph(session)
    customer = make_customer(session, business)
    session.flush()

    result = get_owned_or_404(session, Customer, customer.id, business)
    assert result.id == customer.id


def test_get_selling_option_for_product_missing_raises_not_found(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    session.flush()

    with pytest.raises(NotFoundError):
        get_selling_option_for_product(session, product, uuid.uuid4())


def test_get_selling_option_for_product_belonging_to_different_product_raises_not_found(session):
    business = make_business_graph(session)
    product_a = make_product(session, business, name="Product A")
    product_b = make_product(session, business, name="Product B")
    option_of_b = make_selling_option(session, business, product_b)
    session.flush()

    # Same tenant, wrong product — must still 404, not leak a cross-product row.
    with pytest.raises(NotFoundError):
        get_selling_option_for_product(session, product_a, option_of_b.id)


def test_get_selling_option_for_product_owned_is_returned(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    option = make_selling_option(session, business, product)
    session.flush()

    result = get_selling_option_for_product(session, product, option.id)
    assert result.id == option.id


# --- Phase 4: Product-lock, Recipe/RecipeRevision, Ingredient-lock helpers (Plan v4 §6) ---


def test_get_product_for_business_locked_missing_raises_not_found(session):
    business = make_business_graph(session)
    session.flush()

    with pytest.raises(NotFoundError):
        get_product_for_business_locked(session, uuid.uuid4(), business)


def test_get_product_for_business_locked_foreign_tenant_raises_not_found(session):
    owner_business = make_business_graph(session)
    other_business = make_business_graph(session)
    foreign_product = make_product(session, other_business)
    session.flush()

    with pytest.raises(NotFoundError):
        get_product_for_business_locked(session, foreign_product.id, owner_business)


def test_get_product_for_business_locked_owned_is_returned(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    session.flush()

    result = get_product_for_business_locked(session, product.id, business)
    assert result.id == product.id


def test_get_recipe_for_product_missing_raises_not_found(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    session.flush()

    with pytest.raises(NotFoundError):
        get_recipe_for_product(session, product)


def test_get_recipe_for_product_owned_is_returned(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    recipe = make_recipe(session, business, product)
    session.flush()

    result = get_recipe_for_product(session, product)
    assert result.id == recipe.id


def test_lock_recipe_for_product_owned_is_returned(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    recipe = make_recipe(session, business, product)
    session.flush()

    result = lock_recipe_for_product(session, product)
    assert result.id == recipe.id


def test_lock_recipe_for_product_missing_raises_not_found(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    session.flush()

    with pytest.raises(NotFoundError):
        lock_recipe_for_product(session, product)


def test_get_recipe_revision_for_recipe_missing_raises_not_found(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    recipe = make_recipe(session, business, product)
    session.flush()

    with pytest.raises(NotFoundError):
        get_recipe_revision_for_recipe(session, recipe, uuid.uuid4())


def test_get_recipe_revision_for_recipe_belonging_to_different_recipe_raises_not_found(session):
    business = make_business_graph(session)
    product_a = make_product(session, business, name="Product A")
    product_b = make_product(session, business, name="Product B")
    recipe_a = make_recipe(session, business, product_a, name="Recipe A")
    recipe_b = make_recipe(session, business, product_b, name="Recipe B")
    revision_of_b = make_recipe_revision(session, business, recipe_b)
    session.flush()

    # Same tenant, wrong recipe — must still 404, proving the recipe_id predicate (not
    # just business_id) is doing real work.
    with pytest.raises(NotFoundError):
        get_recipe_revision_for_recipe(session, recipe_a, revision_of_b.id)


def test_get_recipe_revision_for_recipe_owned_is_returned(session):
    business = make_business_graph(session)
    product = make_product(session, business)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe)
    session.flush()

    result = get_recipe_revision_for_recipe(session, recipe, revision.id)
    assert result.id == revision.id


def test_lock_ingredients_for_business_resolves_only_owned_ids(session):
    owner_business = make_business_graph(session)
    other_business = make_business_graph(session)
    owned = make_ingredient(session, owner_business, name="Owned")
    foreign = make_ingredient(session, other_business, name="Foreign")
    session.flush()

    result = lock_ingredients_for_business(
        session, [owned.id, foreign.id, uuid.uuid4()], owner_business
    )
    assert set(result.keys()) == {owned.id}
    assert result[owned.id].id == owned.id


def test_lock_ingredients_for_business_empty_input_returns_empty_dict(session):
    business = make_business_graph(session)
    session.flush()

    assert lock_ingredients_for_business(session, [], business) == {}
