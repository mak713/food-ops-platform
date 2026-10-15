"""Integration tests for manual Surplus creation and adjustment (SUR-008; Phase 8
Final Architecture Lock §9, corrected by Final Architecture Amendment §3 and Final
Architecture Lock Addendum §B, and by the Independent Implementation Review
Remediation Plan v1/v1.1/v1.2 — the active-run hard floor, targeted READY-Surplus
reconciliation, and the acknowledgment guard's reduced-allocation-line scope)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductionRunStatus, ProductType
from app.db.models.production import ProductionRequirement, ProductionRunOrderAllocation
from app.db.models.surplus import SurplusAllocation
from app.schemas.inventory import ManualAdjustmentReason
from app.schemas.surplus import SurplusAdjustmentRequest, SurplusManualCreateRequest
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.readiness_service import evaluate_and_apply_readiness
from app.services.surplus_service import adjust_surplus, create_surplus_manual
from tests.integration.schema.factories import (
    make_business_graph,
    make_order,
    make_order_line,
    make_product,
    make_production_run,
    make_production_run_order_allocation,
    make_recipe,
    make_recipe_revision,
    make_surplus_allocation,
    make_surplus_inventory,
)

_TODAY = date(2026, 6, 1)


def test_manual_creation_requires_no_run_provenance(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    session.commit()

    lot = create_surplus_manual(
        session,
        business,
        SurplusManualCreateRequest(
            product_id=product.id, quantity=Decimal("5"), unit_cost_basis=Decimal("1.50")
        ),
        business_today=_TODAY,
    )
    session.commit()

    assert lot.source_production_run_id is None
    assert lot.source_total_cost_basis is None
    assert lot.unit_cost_basis == Decimal("1.500000")
    assert lot.physical_quantity == Decimal("5.000000")


def test_manual_creation_promotes_waiting_order(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("5"),
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    create_surplus_manual(
        session,
        business,
        SurplusManualCreateRequest(
            product_id=product.id,
            quantity=Decimal("5"),
            unit_cost_basis=Decimal("1.00"),
            is_reusable=True,
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.READY


def test_manual_creation_requires_produced_product(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    session.commit()

    with pytest.raises(ApiError) as exc_info:
        create_surplus_manual(
            session,
            business,
            SurplusManualCreateRequest(
                product_id=product.id, quantity=Decimal("5"), unit_cost_basis=Decimal("1.00")
            ),
            business_today=_TODAY,
        )
    assert exc_info.value.code == "SURPLUS_REQUIRES_PRODUCED_PRODUCT"


def test_positive_adjustment_rejected_by_schema():
    with pytest.raises(ValidationError):
        SurplusAdjustmentRequest(
            version=1,
            quantity_delta=Decimal("5"),
            reason=ManualAdjustmentReason.COUNT_CORRECTION,
        )


def test_adjustment_within_slack_cheap_path(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    session.commit()
    lot = create_surplus_manual(
        session,
        business,
        SurplusManualCreateRequest(
            product_id=product.id, quantity=Decimal("10"), unit_cost_basis=Decimal("1.00")
        ),
        business_today=_TODAY,
    )
    session.commit()

    updated = adjust_surplus(
        session,
        business,
        lot.id,
        SurplusAdjustmentRequest(
            version=lot.version,
            quantity_delta=Decimal("-4"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
        ),
        business_today=_TODAY,
    )
    session.commit()

    assert updated.physical_quantity == Decimal("6.000000")
    # source_total_cost_basis untouched (manual lot -- stays NULL, never fabricated).
    assert updated.source_total_cost_basis is None


def test_adjustment_insufficient_balance_rejected(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    session.commit()
    lot = create_surplus_manual(
        session,
        business,
        SurplusManualCreateRequest(
            product_id=product.id, quantity=Decimal("3"), unit_cost_basis=Decimal("1.00")
        ),
        business_today=_TODAY,
    )
    session.commit()

    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot.id,
            SurplusAdjustmentRequest(
                version=lot.version,
                quantity_delta=Decimal("-5"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    assert exc_info.value.code == "SURPLUS_ADJUSTMENT_INSUFFICIENT_BALANCE"


def test_adjustment_below_allocated_requires_acknowledgment_then_demotes(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("5"),
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    lot = create_surplus_manual(
        session,
        business,
        SurplusManualCreateRequest(
            product_id=product.id,
            quantity=Decimal("5"),
            unit_cost_basis=Decimal("1.00"),
            is_reusable=True,
        ),
        business_today=_TODAY,
    )
    session.commit()

    order = session.get(type(order), order.id)
    evaluate_and_apply_readiness(session, business, order)
    session.commit()
    assert order.status == OrderStatus.READY

    session.refresh(lot)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot.id,
            SurplusAdjustmentRequest(
                version=lot.version,
                quantity_delta=Decimal("-5"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    assert exc_info.value.code == "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT"
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]

    session.refresh(lot)
    session.refresh(order)
    assert order.status == OrderStatus.READY  # untouched by the rejected attempt
    assert lot.physical_quantity == Decimal("5.000000")

    adjust_surplus(
        session,
        business,
        lot.id,
        SurplusAdjustmentRequest(
            version=lot.version,
            quantity_delta=Decimal("-5"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            acknowledged_warning_fingerprint=fingerprint,
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED
    rebuilt = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert rebuilt.production_demand_quantity == Decimal("5.000000")


# --- Remediation Plan v1.2 §3: active-run hard floor -------------------------------


def _produced_order_with_requirement(session: Session, business, *, demand: Decimal):
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=demand,
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    return product, revision, order, line, requirement


def test_active_run_floor_blocks_reduction_below_committed_quantity(session: Session):
    business = make_business_graph(session)
    product, revision, order, line, requirement = _produced_order_with_requirement(
        session, business, demand=Decimal("6")
    )
    make_production_run(
        session,
        business,
        product,
        revision,
        source_production_requirement_id=requirement.id,
        status=ProductionRunStatus.IN_PRODUCTION,
    )
    lot = make_surplus_inventory(session, business, product, physical_quantity=Decimal("10"))
    session.flush()
    make_surplus_allocation(session, business, lot, requirement, order, line, quantity=Decimal("6"))
    session.commit()

    session.refresh(lot)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot.id,
            SurplusAdjustmentRequest(
                version=lot.version,
                quantity_delta=Decimal("-5"),  # 10 -> 5, below the 6-unit committed floor
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    assert exc_info.value.code == "SURPLUS_ADJUSTMENT_PRODUCTION_LOCKED"

    # Zero mutation on rejection: physical quantity, allocation, and Order status
    # all remain exactly as they were.
    session.refresh(lot)
    assert lot.physical_quantity == Decimal("10.000000")
    allocations = session.scalars(
        select(SurplusAllocation).where(SurplusAllocation.surplus_inventory_id == lot.id)
    ).all()
    assert len(allocations) == 1
    assert allocations[0].quantity == Decimal("6.000000")
    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED


def test_active_run_floor_allows_reduction_at_the_floor(session: Session):
    business = make_business_graph(session)
    product, revision, order, line, requirement = _produced_order_with_requirement(
        session, business, demand=Decimal("6")
    )
    make_production_run(
        session,
        business,
        product,
        revision,
        source_production_requirement_id=requirement.id,
        status=ProductionRunStatus.IN_PRODUCTION,
    )
    lot = make_surplus_inventory(session, business, product, physical_quantity=Decimal("10"))
    session.flush()
    make_surplus_allocation(session, business, lot, requirement, order, line, quantity=Decimal("6"))
    session.commit()

    session.refresh(lot)
    updated = adjust_surplus(
        session,
        business,
        lot.id,
        SurplusAdjustmentRequest(
            version=lot.version,
            quantity_delta=Decimal("-4"),  # 10 -> 6, exactly at the committed floor
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
        ),
        business_today=_TODAY,
    )
    session.commit()
    assert updated.physical_quantity == Decimal("6.000000")


# --- Remediation Plan v1.2 §4: acknowledgment scoped to every reduced allocation ---


def test_confirmed_order_allocation_reduction_requires_acknowledgment_without_ready_demotion(
    session: Session,
):
    """A CONFIRMED (never-READY) Order losing part of its existing Surplus
    allocation is consequential even when no READY Order is demoted — the bug this
    round's Finding 8 correction fixes (previously gated on `demoted_order_ids`
    alone)."""
    business = make_business_graph(session)
    product, revision, order, line, requirement = _produced_order_with_requirement(
        session, business, demand=Decimal("10")
    )
    lot = make_surplus_inventory(session, business, product, physical_quantity=Decimal("4"))
    session.flush()
    make_surplus_allocation(session, business, lot, requirement, order, line, quantity=Decimal("4"))
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED  # only 4 of 10 covered -- never READY

    session.refresh(lot)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot.id,
            SurplusAdjustmentRequest(
                version=lot.version,
                quantity_delta=Decimal("-2"),  # 4 -> 2, below the allocated 4
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    assert exc_info.value.code == "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT"
    issue = exc_info.value.issues[0]
    assert issue["details"]["demoted_order_ids"] == []
    assert issue["details"]["reduced_order_line_ids"] == [str(line.id)]

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED  # unchanged by the rejected attempt


def test_stale_fingerprint_does_not_authorize_a_different_consequence(session: Session):
    business = make_business_graph(session)
    product, revision, order, line, requirement = _produced_order_with_requirement(
        session, business, demand=Decimal("10")
    )
    lot = make_surplus_inventory(session, business, product, physical_quantity=Decimal("6"))
    session.flush()
    make_surplus_allocation(session, business, lot, requirement, order, line, quantity=Decimal("6"))
    session.commit()

    session.refresh(lot)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot.id,
            SurplusAdjustmentRequest(
                version=lot.version,
                quantity_delta=Decimal("-5"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    stale_fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]

    session.refresh(lot)
    with pytest.raises(ApiError) as exc_info2:
        adjust_surplus(
            session,
            business,
            lot.id,
            SurplusAdjustmentRequest(
                version=lot.version,
                quantity_delta=Decimal("-4"),  # a different delta -> a different consequence
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
                acknowledged_warning_fingerprint=stale_fingerprint,
            ),
            business_today=_TODAY,
        )
    assert exc_info2.value.code == "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT"
    assert exc_info2.value.issues[0]["details"]["fingerprint"] != stale_fingerprint


# --- Remediation Plan v1.2 §2: targeted READY-Surplus reconciliation ---------------


def test_targeted_reconciliation_preserves_unaffected_lot_allocation(session: Session):
    """READY Order A holds lot L1, READY Order B holds lot L2 -- reducing L1 must
    never expose B's L2 claim to ordinary FEFO competition merely because both
    lots belong to the same Product."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))

    order_a = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_a.fulfillment_date = _TODAY
    line_a = make_order_line(
        session,
        business,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("4"),
    )
    order_b = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_b.fulfillment_date = _TODAY + timedelta(days=1)
    line_b = make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("3"),
    )
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    requirement_a = session.scalars(
        select(ProductionRequirement).where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.demand_date == _TODAY,
        )
    ).one()
    requirement_b = session.scalars(
        select(ProductionRequirement).where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.demand_date == _TODAY + timedelta(days=1),
        )
    ).one()

    lot1 = make_surplus_inventory(session, business, product, physical_quantity=Decimal("4"))
    lot2 = make_surplus_inventory(session, business, product, physical_quantity=Decimal("3"))
    session.flush()
    make_surplus_allocation(
        session, business, lot1, requirement_a, order_a, line_a, quantity=Decimal("4")
    )
    make_surplus_allocation(
        session, business, lot2, requirement_b, order_b, line_b, quantity=Decimal("3")
    )
    session.commit()

    order_a = session.get(type(order_a), order_a.id)
    order_b = session.get(type(order_b), order_b.id)
    evaluate_and_apply_readiness(session, business, order_a)
    evaluate_and_apply_readiness(session, business, order_b)
    session.commit()
    assert order_a.status == OrderStatus.READY
    assert order_b.status == OrderStatus.READY

    session.refresh(lot1)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot1.id,
            SurplusAdjustmentRequest(
                version=lot1.version,
                quantity_delta=Decimal("-4"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]
    assert str(line_b.id) not in exc_info.value.issues[0]["details"]["reduced_order_line_ids"]

    session.refresh(lot1)
    adjust_surplus(
        session,
        business,
        lot1.id,
        SurplusAdjustmentRequest(
            version=lot1.version,
            quantity_delta=Decimal("-4"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            acknowledged_warning_fingerprint=fingerprint,
        ),
        business_today=_TODAY,
    )
    session.commit()

    session.refresh(order_a)
    session.refresh(order_b)
    assert order_a.status == OrderStatus.CONFIRMED  # lost its only supply
    assert order_b.status == OrderStatus.READY  # completely untouched

    lot2_allocations = session.scalars(
        select(SurplusAllocation).where(SurplusAllocation.surplus_inventory_id == lot2.id)
    ).all()
    assert len(lot2_allocations) == 1
    assert lot2_allocations[0].quantity == Decimal("3.000000")
    assert lot2_allocations[0].order_line_id == line_b.id


def test_targeted_reconciliation_releases_only_impaired_lot_portion_of_split_line(
    session: Session,
):
    """One READY line covered by two lots (L1 + L2) -- reducing L1 must release
    only the L1-backed quantity; the L2-backed portion remains fixed, and the
    allocator is only ever asked for the genuinely-released remainder."""
    business = make_business_graph(session)
    product, revision, order, line, requirement = _produced_order_with_requirement(
        session, business, demand=Decimal("7")
    )
    lot1 = make_surplus_inventory(session, business, product, physical_quantity=Decimal("4"))
    lot2 = make_surplus_inventory(session, business, product, physical_quantity=Decimal("3"))
    session.flush()
    make_surplus_allocation(
        session, business, lot1, requirement, order, line, quantity=Decimal("4")
    )
    make_surplus_allocation(
        session, business, lot2, requirement, order, line, quantity=Decimal("3")
    )
    session.commit()

    order = session.get(type(order), order.id)
    evaluate_and_apply_readiness(session, business, order)
    session.commit()
    assert order.status == OrderStatus.READY  # 4 + 3 == 7, fully covered

    session.refresh(lot1)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot1.id,
            SurplusAdjustmentRequest(
                version=lot1.version,
                quantity_delta=Decimal("-4"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]

    session.refresh(lot1)
    adjust_surplus(
        session,
        business,
        lot1.id,
        SurplusAdjustmentRequest(
            version=lot1.version,
            quantity_delta=Decimal("-4"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            acknowledged_warning_fingerprint=fingerprint,
        ),
        business_today=_TODAY,
    )
    session.commit()

    remaining = session.scalars(
        select(SurplusAllocation).where(SurplusAllocation.order_line_id == line.id)
    ).all()
    total = sum((a.quantity for a in remaining), Decimal(0))
    assert total == Decimal("3.000000")
    assert {a.surplus_inventory_id for a in remaining} == {lot2.id}

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED
    rebuilt = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    # 7 - 3 (still-fixed L2) == 4 -- exactly the released L1 portion, never the
    # full original 7.
    assert rebuilt.production_demand_quantity == Decimal("4.000000")


def test_targeted_reconciliation_composition_with_completed_production_and_unaffected_lot(
    session: Session,
):
    """New required composition regression: one READY produced line satisfied by
    (a) immutable completed ProductionRun output, (b) an unaffected READY Surplus
    lot, and (c) an impaired READY Surplus lot. Reducing the impaired lot must
    release only its own portion -- completed-production coverage and the
    unaffected lot's coverage must both remain exactly untouched, with no phantom
    production/Ingredient-reservation demand for the already-satisfied portion."""
    business = make_business_graph(session)
    product, revision, order, line, requirement = _produced_order_with_requirement(
        session, business, demand=Decimal("10")
    )

    completed_run = make_production_run(
        session, business, product, revision, status=ProductionRunStatus.COMPLETED
    )
    session.flush()
    make_production_run_order_allocation(
        session, business, completed_run, order, line, quantity=Decimal("3")
    )

    lot_impaired = make_surplus_inventory(
        session, business, product, physical_quantity=Decimal("4")
    )
    lot_unaffected = make_surplus_inventory(
        session, business, product, physical_quantity=Decimal("3")
    )
    session.flush()
    make_surplus_allocation(
        session, business, lot_impaired, requirement, order, line, quantity=Decimal("4")
    )
    make_surplus_allocation(
        session, business, lot_unaffected, requirement, order, line, quantity=Decimal("3")
    )
    session.commit()
    # 3 (completed) + 4 (impaired) + 3 (unaffected) == 10 -- fully satisfied.

    order = session.get(type(order), order.id)
    evaluate_and_apply_readiness(session, business, order)
    session.commit()
    assert order.status == OrderStatus.READY

    session.refresh(lot_impaired)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot_impaired.id,
            SurplusAdjustmentRequest(
                version=lot_impaired.version,
                quantity_delta=Decimal("-4"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]

    session.refresh(lot_impaired)
    adjust_surplus(
        session,
        business,
        lot_impaired.id,
        SurplusAdjustmentRequest(
            version=lot_impaired.version,
            quantity_delta=Decimal("-4"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            acknowledged_warning_fingerprint=fingerprint,
        ),
        business_today=_TODAY,
    )
    session.commit()

    # Historical/immutable -- untouched.
    completed_allocations = session.scalars(
        select(ProductionRunOrderAllocation).where(
            ProductionRunOrderAllocation.order_line_id == line.id
        )
    ).all()
    assert len(completed_allocations) == 1
    assert completed_allocations[0].quantity == Decimal("3.000000")

    # Unaffected lot's allocation untouched.
    remaining_surplus = session.scalars(
        select(SurplusAllocation).where(SurplusAllocation.order_line_id == line.id)
    ).all()
    total_surplus = sum((a.quantity for a in remaining_surplus), Decimal(0))
    assert total_surplus == Decimal("3.000000")
    assert {a.surplus_inventory_id for a in remaining_surplus} == {lot_unaffected.id}

    # Remaining production demand = 10 - 3 (completed) - 3 (unaffected surplus)
    # == 4 -- exactly the released impaired-lot portion, never double-subtracted
    # or double-counted against the completed/unaffected coverage.
    rebuilt = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert rebuilt.production_demand_quantity == Decimal("4.000000")

    session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED
