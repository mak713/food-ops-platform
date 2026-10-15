"""Integration tests for `production_run_service.finish_production` (RUN-007..012;
Phase 8 Final Architecture Amendment §1, corrected by the Independent
Implementation Review Remediation Plan v1/v1.2 §7/Finding 10 — the Finish
persistence-world quantize-before-branch correction — and the unknown-Ingredient-
override rejection)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import (
    InventoryTransactionType,
    OrderLineType,
    OrderStatus,
    ProductionRunStatus,
    ProductType,
)
from app.db.models.cost import OrderCostAllocation
from app.db.models.ingredient import InventoryTransaction
from app.db.models.production import ProductionRequirement, ProductionRunOrderAllocation
from app.db.models.surplus import SurplusInventory, SurplusTransaction
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.production_run_service import finish_production, start_production
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_production_run_ingredient,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)

_TODAY = date(2026, 6, 1)


def _started_run(
    session: Session,
    *,
    demand_quantity=Decimal("6"),
    yield_quantity=Decimal("12"),
    ingredient_quantity=Decimal("500"),
    fulfillment_date=_TODAY,
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
        session, business, revision, ingredient, quantity=ingredient_quantity
    )
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = fulfillment_date
    line = make_order_line(
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
    return business, product, ingredient, order, line, run


def test_finish_exact_output_fully_allocates_no_surplus(session: Session):
    business, product, ingredient, order, line, run = _started_run(session)

    # Actual output matches demand exactly (6 of the 12 planned) -- a genuine
    # "no surplus" scenario; the batch-plan's other 6 units are simply not produced.
    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("6"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    assert result.run.status == ProductionRunStatus.COMPLETED
    assert result.run.completed_at is not None
    assert result.run.actual_output_quantity == Decimal("6.000000")
    assert result.negative_inventory_ingredient_ids == []

    allocations = session.scalars(
        select(ProductionRunOrderAllocation).where(
            ProductionRunOrderAllocation.production_run_id == run.id
        )
    ).all()
    assert len(allocations) == 1
    assert allocations[0].order_line_id == line.id
    assert allocations[0].quantity == Decimal("6.000000")

    cost_allocations = session.scalars(
        select(OrderCostAllocation).where(OrderCostAllocation.order_id == order.id)
    ).all()
    assert len(cost_allocations) == 1
    assert cost_allocations[0].amount == result.run.actual_total_production_cost

    assert (
        session.scalars(
            select(SurplusInventory).where(SurplusInventory.source_production_run_id == run.id)
        ).first()
        is None
    )

    # Ingredient physically decremented + transaction recorded.
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("500.000000")  # 1000 - 500
    txn = session.scalars(
        select(InventoryTransaction).where(InventoryTransaction.production_run_id == run.id)
    ).one()
    assert txn.transaction_type == InventoryTransactionType.PRODUCTION_CONSUMPTION
    assert txn.quantity_change == Decimal("-500.000000")


def test_finish_with_excess_creates_surplus_with_exact_source_total(session: Session):
    business, product, ingredient, order, line, run = _started_run(
        session, demand_quantity=Decimal("6")
    )
    # planned_batches=1, planned_output=12; demand=6 -> 6 excess.

    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("12"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    surplus = session.scalars(
        select(SurplusInventory).where(SurplusInventory.source_production_run_id == run.id)
    ).one()
    assert surplus.physical_quantity == Decimal("6.000000")
    assert surplus.source_total_cost_basis is not None
    assert surplus.is_reusable == product.can_reuse_surplus

    cost_allocations = session.scalars(
        select(OrderCostAllocation).where(OrderCostAllocation.order_id == order.id)
    ).all()
    # Exact invariant: direct allocations + surplus source-total == run total cost.
    total = sum((c.amount for c in cost_allocations), Decimal(0)) + surplus.source_total_cost_basis
    assert total == result.run.actual_total_production_cost

    surplus_txn = session.scalars(
        select(SurplusTransaction).where(SurplusTransaction.surplus_inventory_id == surplus.id)
    ).one()
    assert surplus_txn.quantity_change == Decimal("6.000000")


def test_finish_zero_output_still_consumes_ingredients_no_allocation_no_surplus(
    session: Session,
):
    business, product, ingredient, order, line, run = _started_run(session)

    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("0"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    assert result.run.status == ProductionRunStatus.COMPLETED
    assert result.run.actual_output_quantity == Decimal("0.000000")
    assert result.is_shortfall is True

    # Ingredient consumption still occurred (RUN-009 — never conditional on output).
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("500.000000")
    txn = session.scalars(
        select(InventoryTransaction).where(InventoryTransaction.production_run_id == run.id)
    ).one()
    assert txn.quantity_change == Decimal("-500.000000")

    assert (
        session.scalars(
            select(ProductionRunOrderAllocation).where(
                ProductionRunOrderAllocation.production_run_id == run.id
            )
        ).first()
        is None
    )
    assert (
        session.scalars(
            select(OrderCostAllocation).where(OrderCostAllocation.order_id == order.id)
        ).first()
        is None
    )
    assert (
        session.scalars(
            select(SurplusInventory).where(SurplusInventory.source_production_run_id == run.id)
        ).first()
        is None
    )

    # actual cost was still recorded (real ingredients/labor spent on a failed batch).
    assert result.run.actual_total_production_cost > 0

    # RUN-012: the full demand remains/returns as active shortfall.
    rebuilt = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert rebuilt.production_demand_quantity == Decimal("6.000000")


def test_finish_underproduction_leaves_shortfall_and_partial_allocation(session: Session):
    business, product, ingredient, order, line, run = _started_run(
        session, demand_quantity=Decimal("10")
    )
    # planned_output=12 >= 10, no acknowledgment needed at Start. Actual output=4 <
    # demand=10 -> partial allocation + remaining shortfall.

    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("4"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    allocation = session.scalars(
        select(ProductionRunOrderAllocation).where(
            ProductionRunOrderAllocation.production_run_id == run.id
        )
    ).one()
    assert allocation.quantity == Decimal("4.000000")
    assert result.is_shortfall is True

    rebuilt = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    # Original demand 10, 4 already allocated by this completed run -> 6 remaining.
    assert rebuilt.production_demand_quantity == Decimal("6.000000")


def test_finish_ingredient_usage_adjustment_and_negative_balance_allowed(session: Session):
    business, product, ingredient, order, line, run = _started_run(session)
    ingredient_id = ingredient.id

    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("12"),
        ingredient_actual_quantities={ingredient_id: Decimal("1200")},  # more than on hand
        business_today=_TODAY,
    )
    session.commit()

    assert ingredient_id in result.negative_inventory_ingredient_ids
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("-200.000000")  # 1000 - 1200, never clamped


def test_double_finish_rejected(session: Session):
    business, product, ingredient, order, line, run = _started_run(session)
    finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("12"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()
    session.refresh(run)

    with pytest.raises(ApiError) as exc_info:
        finish_production(
            session,
            business,
            run.id,
            expected_version=run.version,
            actual_output_quantity=Decimal("12"),
            ingredient_actual_quantities=None,
            business_today=_TODAY,
        )
    assert exc_info.value.code == "PRODUCTION_RUN_NOT_IN_PRODUCTION"


def test_finish_promotes_order_to_ready_when_fully_satisfied(session: Session):
    business, product, ingredient, order, line, run = _started_run(session)

    finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("12"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.READY
    assert order.ready_at is not None


def test_finish_does_not_promote_when_underproduced(session: Session):
    business, product, ingredient, order, line, run = _started_run(
        session, demand_quantity=Decimal("10")
    )

    finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("4"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED


def test_finish_sub_quantum_positive_output_takes_zero_output_path(session: Session):
    """Remediation Plan v1.2 §7/Finding 10: `actual_output_quantity` is quantized
    exactly once, immediately, at the persistence-world boundary, and every
    subsequent read (including the positive/zero-output branch decision) uses that
    quantized value — never the raw input. `Decimal("0.0000004")` is positive as a
    raw Decimal but quantizes to `0.000000` at NUMERIC(18,6); this must take the
    already-approved zero-output path (ingredient consumption/cost/history still
    occur; no output allocation or Surplus is created) rather than reaching the
    apportioner with an effectively-zero quantity and raising an uncaught
    exception."""
    business, product, ingredient, order, line, run = _started_run(session)

    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("0.0000004"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    assert result.run.status == ProductionRunStatus.COMPLETED
    assert result.run.actual_output_quantity == Decimal("0.000000")
    assert result.is_shortfall is True

    # Ingredient consumption/cost still occurred, exactly like the ordinary
    # zero-output case.
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("500.000000")
    assert result.run.actual_total_production_cost > 0

    # No output allocation, no Surplus -- the zero-output branch, not a crash.
    assert (
        session.scalars(
            select(ProductionRunOrderAllocation).where(
                ProductionRunOrderAllocation.production_run_id == run.id
            )
        ).first()
        is None
    )
    assert (
        session.scalars(
            select(SurplusInventory).where(SurplusInventory.source_production_run_id == run.id)
        ).first()
        is None
    )


def test_finish_normal_positive_six_decimal_output_unaffected(session: Session):
    """The quantize-before-branch correction must not change ordinary positive-output
    behavior at all."""
    business, product, ingredient, order, line, run = _started_run(session)

    result = finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("6.123456"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    assert result.run.status == ProductionRunStatus.COMPLETED
    assert result.run.actual_output_quantity == Decimal("6.123456")
    assert result.is_shortfall is False  # 6.123456 > demand of 6 -- fully covered + tiny excess
    allocation = session.scalars(
        select(ProductionRunOrderAllocation).where(
            ProductionRunOrderAllocation.production_run_id == run.id
        )
    ).one()
    assert allocation.quantity == Decimal("6.000000")
    surplus = session.scalars(
        select(SurplusInventory).where(SurplusInventory.source_production_run_id == run.id)
    ).one()
    assert surplus.physical_quantity == Decimal("0.123456")


def test_finish_unknown_ingredient_override_rejected(session: Session):
    business, product, ingredient, order, line, run = _started_run(session)

    with pytest.raises(ApiError) as exc_info:
        finish_production(
            session,
            business,
            run.id,
            expected_version=run.version,
            actual_output_quantity=Decimal("6"),
            ingredient_actual_quantities={uuid.uuid4(): Decimal("10")},
            business_today=_TODAY,
        )
    assert exc_info.value.code == "PRODUCTION_FINISH_UNKNOWN_INGREDIENT_OVERRIDE"

    # Zero mutation -- the run is still IN_PRODUCTION, nothing consumed.
    session.refresh(run)
    assert run.status == ProductionRunStatus.IN_PRODUCTION
    session.refresh(ingredient)
    assert ingredient.physical_quantity == Decimal("1000.000000")


def test_finish_never_reads_a_cross_business_production_run_ingredient(session: Session):
    """Follow-up review item 2 (SEC-001): a malformed child row owned by Business B
    that merely references Business A's run id must never be read, consumed, or
    accepted as an override target by Business A's Finish."""
    business, product, ingredient, order, line, run = _started_run(session)

    other_business = make_business_graph(session)
    foreign_ingredient = make_ingredient(session, other_business)
    foreign_ingredient.physical_quantity = Decimal("1000")
    foreign_child = make_production_run_ingredient(
        session, other_business, run, foreign_ingredient, planned_quantity=500
    )
    session.commit()
    foreign_child_id = foreign_child.id

    # The foreign ingredient is not part of this run for Business A.
    with pytest.raises(ApiError) as exc_info:
        finish_production(
            session,
            business,
            run.id,
            expected_version=run.version,
            actual_output_quantity=Decimal("6"),
            ingredient_actual_quantities={foreign_ingredient.id: Decimal("10")},
            business_today=_TODAY,
        )
    assert exc_info.value.code == "PRODUCTION_FINISH_UNKNOWN_INGREDIENT_OVERRIDE"

    session.refresh(run)
    finish_production(
        session,
        business,
        run.id,
        expected_version=run.version,
        actual_output_quantity=Decimal("6"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(foreign_ingredient)
    assert foreign_ingredient.physical_quantity == Decimal("1000.000000")
    consumed_foreign = session.scalars(
        select(InventoryTransaction).where(
            InventoryTransaction.ingredient_id == foreign_ingredient.id
        )
    ).all()
    assert consumed_foreign == []

    from app.db.models.production import ProductionRunIngredient

    untouched = session.get(ProductionRunIngredient, foreign_child_id)
    assert untouched.actual_quantity is None
    session.refresh(ingredient)
    assert ingredient.physical_quantity < Decimal("1000.000000")
