"""Integration tests for `production_run_service.cancel_production_run` (Phase 8
Final Architecture Lock §4/§11, corrected by the Independent Implementation Review
Remediation Plan v1.2 §1 — Cancel Production Run may change another Order's
readiness by releasing an `IN_PRODUCTION`-fixed Surplus claim)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductionRunStatus, ProductType
from app.db.models.order import Order, OrderLine
from app.db.models.production import (
    ProductionRequirement,
    ProductionRequirementOrder,
    ProductionRun,
)
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.production_run_service import cancel_production_run, start_production
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
    make_surplus_allocation,
    make_surplus_inventory,
)

_TODAY = date(2026, 6, 1)


def _started_run(session: Session, *, demand_quantity=Decimal("6")):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session, business, recipe, yield_quantity=Decimal("12"), active_time_minutes=30
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
    session.commit()

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    run = start_production(
        session, business, requirement.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()
    return business, product, ingredient, requirement, run


def test_cancel_marks_canceled_no_ingredient_touched(session: Session):
    business, product, ingredient, requirement, run = _started_run(session)
    physical_before = ingredient.physical_quantity

    canceled_run, result = cancel_production_run(
        session, business, run.id, expected_version=run.version, business_today=_TODAY
    )
    session.commit()

    assert canceled_run.status == ProductionRunStatus.CANCELED
    assert canceled_run.canceled_at is not None
    assert canceled_run.completed_at is None
    session.refresh(ingredient)
    assert ingredient.physical_quantity == physical_before
    assert result.product_id == product.id


def test_cancel_frees_demand_for_immediate_recalculation(session: Session):
    """The freed demand reappears as an active (rebuildable) ProductionRequirement
    right away, not stale until an unrelated future event."""
    business, product, _ingredient, requirement, run = _started_run(session)

    cancel_production_run(
        session, business, run.id, expected_version=run.version, business_today=_TODAY
    )
    session.commit()

    rebuilt = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert rebuilt.production_demand_quantity == Decimal("6.000000")
    assert rebuilt.calculation_status.value == "CALCULATED"


def test_cancel_only_allowed_while_in_production(session: Session):
    business, product, _ingredient, requirement, run = _started_run(session)
    cancel_production_run(
        session, business, run.id, expected_version=run.version, business_today=_TODAY
    )
    session.commit()
    session.refresh(run)

    with pytest.raises(ApiError) as exc_info:
        cancel_production_run(
            session, business, run.id, expected_version=run.version, business_today=_TODAY
        )
    assert exc_info.value.code == "PRODUCTION_RUN_NOT_IN_PRODUCTION"


def test_cancel_rejects_stale_version(session: Session):
    business, product, _ingredient, requirement, run = _started_run(session)
    with pytest.raises(ApiError) as exc_info:
        cancel_production_run(
            session, business, run.id, expected_version=run.version + 1, business_today=_TODAY
        )
    assert exc_info.value.code == "STALE_VERSION"

    # Untouched — the run is still IN_PRODUCTION.
    fresh = session.scalars(select(ProductionRun).where(ProductionRun.id == run.id)).one()
    assert fresh.status == ProductionRunStatus.IN_PRODUCTION


def test_cancel_releases_fixed_surplus_claim_and_promotes_higher_priority_order(
    session: Session,
):
    """Independent Implementation Review Remediation Plan v1.2 §1's exact scenario:
    while `run` is `IN_PRODUCTION`, its source requirement's Surplus allocation is
    fixed/excluded from ordinary FEFO competition; a second, higher-FEFO-priority
    (earlier-deadline) CONFIRMED Order for the same Product cannot take that fixed
    claim. Canceling the run must release it and let the higher-priority Order
    claim it, promoting it to READY, within Cancel's own transaction."""
    business, product, _ingredient, requirement, run = _started_run(session)
    link = session.scalars(
        select(ProductionRequirementOrder).where(
            ProductionRequirementOrder.production_requirement_id == requirement.id
        )
    ).one()
    order_a = session.get(Order, link.order_id)
    line_a = session.get(OrderLine, link.order_line_id)

    lot = make_surplus_inventory(session, business, product, physical_quantity=Decimal("6"))
    session.flush()
    # The lot's entire capacity is already committed to the protected requirement
    # (simulating Surplus that was allocated before the run started).
    make_surplus_allocation(
        session, business, lot, requirement, order_a, line_a, quantity=Decimal("6")
    )
    session.commit()

    order_b = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_b.fulfillment_date = _TODAY - timedelta(days=1)  # earlier deadline than order A
    make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    requirement_b = session.scalars(
        select(ProductionRequirement).where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.demand_date == order_b.fulfillment_date,
        )
    ).one()
    # B cannot touch the fixed claim while the run is IN_PRODUCTION -- the lot's
    # entire capacity is unavailable to it.
    assert requirement_b.production_demand_quantity == Decimal("6.000000")
    assert order_b.status == OrderStatus.CONFIRMED

    cancel_production_run(
        session, business, run.id, expected_version=run.version, business_today=_TODAY
    )
    session.commit()

    session.refresh(order_b)
    rebuilt_b = session.scalars(
        select(ProductionRequirement).where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.demand_date == order_b.fulfillment_date,
        )
    ).one()
    # The fixed constraint is gone -- B (earlier deadline) now claims the freed lot
    # and is promoted to READY, within Cancel's own transaction.
    assert rebuilt_b.production_demand_quantity == Decimal("0.000000")
    assert order_b.status == OrderStatus.READY
