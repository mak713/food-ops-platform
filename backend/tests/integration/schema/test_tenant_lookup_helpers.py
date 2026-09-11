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

from app.core.tenant import NotFoundError, get_owned_or_404, get_selling_option_for_product
from app.db.models.customer import Customer
from tests.integration.schema.factories import (
    make_business_graph,
    make_customer,
    make_product,
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
