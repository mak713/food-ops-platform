"""Integration tests for `production_run_service.start_production` against a real
PostgreSQL test database (Phase 8 Final Architecture Lock §5 / Amendment §1)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import (
    CostBasisSource,
    OrderLineType,
    OrderStatus,
    ProductionRunStatus,
    ProductType,
)
from app.db.models.production import ProductionRequirement, ProductionRun, ProductionRunIngredient
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


def _confirmed_setup(
    session: Session, *, demand_quantity=Decimal("6"), yield_quantity=Decimal("12")
):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session, business, recipe, yield_quantity=yield_quantity, active_time_minutes=30
    )
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("1000")
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=demand_quantity,
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    # Committed, not merely flushed: represents a realistic already-confirmed Order
    # whose demand was recalculated in a prior, separate, already-committed
    # transaction — so a later rollback inside `start_production` (e.g. an
    # unacknowledged-warning rejection) never wipes out this fixture data, exactly
    # as a real prior transaction's commit never would.
    session.commit()
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    return business, product, revision, ingredient, requirement


def test_start_happy_path_creates_in_production_run(session: Session):
    business, product, revision, ingredient, requirement = _confirmed_setup(session)

    run = start_production(
        session,
        business,
        requirement.id,
        batches=None,
        acknowledged_warning_fingerprint=None,
    )
    session.commit()

    assert run.status == ProductionRunStatus.IN_PRODUCTION
    assert run.source_production_requirement_id == requirement.id
    assert run.product_id == product.id
    assert run.recipe_revision_id == revision.id
    assert run.planned_batches == 1  # ceil(6/12)
    assert run.planned_output_quantity == Decimal("12.000000")
    assert run.started_at is not None
    assert run.completed_at is None
    assert run.canceled_at is None
    # Ingredient physical_quantity is untouched by Start (RUN-006).
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("1000.000000")

    run_ingredients = session.scalars(
        select(ProductionRunIngredient).where(ProductionRunIngredient.production_run_id == run.id)
    ).all()
    assert len(run_ingredients) == 1
    ri = run_ingredients[0]
    assert ri.planned_quantity == Decimal("500.000000")  # 500g/batch * 1 batch
    assert ri.weighted_average_unit_cost_snapshot == Decimal("0.020000")
    assert ri.cost_basis_source == CostBasisSource.WAC


def test_start_uses_fallback_when_physical_quantity_not_positive(session: Session):
    business, product, _revision, ingredient, requirement = _confirmed_setup(session)
    ingredient.physical_quantity = Decimal("0")
    ingredient.replacement_unit_cost = Decimal("0.05")
    session.flush()

    run = start_production(
        session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()

    ri = session.scalars(
        select(ProductionRunIngredient).where(ProductionRunIngredient.production_run_id == run.id)
    ).one()
    assert ri.cost_basis_source == CostBasisSource.FALLBACK
    assert ri.replacement_unit_cost_snapshot == Decimal("0.050000")


def test_start_rejects_unknown_cost_basis(session: Session):
    business, product, _revision, ingredient, requirement = _confirmed_setup(session)
    ingredient.physical_quantity = Decimal("0")
    ingredient.replacement_unit_cost = None
    ingredient.latest_purchase_unit_cost = None
    session.flush()

    with pytest.raises(ApiError) as exc_info:
        start_production(
            session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
        )
    assert exc_info.value.code == "PRODUCTION_COST_BASIS_UNKNOWN"
    assert exc_info.value.status_code == 409

    session.rollback()
    assert (
        session.scalars(
            select(ProductionRun).where(ProductionRun.business_id == business.id)
        ).first()
        is None
    )


def test_start_twice_for_same_requirement_rejected(session: Session):
    business, product, _revision, _ingredient, requirement = _confirmed_setup(session)
    start_production(
        session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()

    with pytest.raises(ApiError) as exc_info:
        start_production(
            session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
        )
    assert exc_info.value.code == "PRODUCTION_ALREADY_STARTED"


def test_zero_batches_rejected_as_structurally_invalid(session: Session):
    business, _product, _revision, _ingredient, requirement = _confirmed_setup(session)
    with pytest.raises(ApiError) as exc_info:
        start_production(
            session, business, requirement.id, batches=0, acknowledged_warning_fingerprint=None
        )
    assert exc_info.value.code == "PRODUCTION_BATCHES_MUST_BE_POSITIVE"


def test_underproduction_requires_acknowledgment_then_succeeds_with_matching_fingerprint(
    session: Session,
):
    # 13 units demanded, yield=12/batch -> recommended 2 batches. Seller forces 1
    # batch (genuine underproduction: 12 < 13).
    business, _product, _revision, _ingredient, requirement = _confirmed_setup(
        session, demand_quantity=Decimal("13"), yield_quantity=Decimal("12")
    )

    with pytest.raises(ApiError) as exc_info:
        start_production(
            session, business, requirement.id, batches=1, acknowledged_warning_fingerprint=None
        )
    assert exc_info.value.code == "UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED"
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]

    # The rejected attempt left nothing committed or half-written.
    assert (
        session.scalars(
            select(ProductionRun).where(ProductionRun.business_id == business.id)
        ).first()
        is None
    )

    run = start_production(
        session,
        business,
        requirement.id,
        batches=1,
        acknowledged_warning_fingerprint=fingerprint,
    )
    session.commit()
    assert run.planned_batches == 1
    assert run.planned_output_quantity == Decimal("12.000000")


def test_stale_fingerprint_rejected_with_fresh_one(session: Session):
    business, product, revision, ingredient, requirement = _confirmed_setup(
        session, demand_quantity=Decimal("13"), yield_quantity=Decimal("12")
    )
    with pytest.raises(ApiError) as exc_info:
        start_production(
            session, business, requirement.id, batches=1, acknowledged_warning_fingerprint=None
        )
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]
    session.rollback()

    # A stale/garbage fingerprint (never matches) is rejected again, with a fresh one
    # supplied for the client to retry against.
    with pytest.raises(ApiError) as exc_info:
        start_production(
            session,
            business,
            requirement.id,
            batches=1,
            acknowledged_warning_fingerprint="not-the-real-fingerprint",
        )
    assert exc_info.value.code == "UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED"
    assert exc_info.value.issues[0]["details"]["fingerprint"] == fingerprint


def test_overproduction_is_notice_only_no_acknowledgment_required(session: Session):
    business, product, revision, ingredient, requirement = _confirmed_setup(
        session, demand_quantity=Decimal("6"), yield_quantity=Decimal("12")
    )
    # Seller overrides to 2 batches (24 units) against 6 units of demand -- pure
    # overproduction, never gated.
    run = start_production(
        session, business, requirement.id, batches=2, acknowledged_warning_fingerprint=None
    )
    session.commit()
    assert run.planned_batches == 2
    assert run.planned_output_quantity == Decimal("24.000000")


def test_pinned_historical_revision_used_even_when_not_current(session: Session):
    """Corrected Start validation (Final Architecture Lock §5): the pinned
    RecipeRevision is used as-is, never compared against the Product's current
    revision."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    old_revision = make_recipe_revision(
        session, business, recipe, revision_number=1, yield_quantity=Decimal("12"), is_current=False
    )
    new_revision = make_recipe_revision(
        session, business, recipe, revision_number=2, yield_quantity=Decimal("20"), is_current=True
    )
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("1000")
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    make_recipe_revision_ingredient(
        session, business, old_revision, ingredient, quantity=Decimal("500")
    )
    make_recipe_revision_ingredient(
        session, business, new_revision, ingredient, quantity=Decimal("700")
    )
    session.flush()

    from tests.integration.schema.factories import make_production_requirement

    requirement = make_production_requirement(
        session,
        business,
        product,
        old_revision,
        confirmed_demand_quantity=Decimal("6"),
        production_demand_quantity=Decimal("6"),
    )
    session.flush()

    run = start_production(
        session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()

    assert run.recipe_revision_id == old_revision.id
    assert run.recipe_yield_snapshot == Decimal("12.000000")
