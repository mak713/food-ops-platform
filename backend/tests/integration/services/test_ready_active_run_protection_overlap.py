"""Follow-up review item 1 (BLOCKER): a SurplusAllocation can be READY-linked AND
`IN_PRODUCTION`-protected at the same time. The stronger active-run axis must
represent it exactly once: never doubled against its lot, never released by
targeted READY reconciliation, never cloned/reparented, and never looked up
through `demand_line_key` (a protected line is absent from `rebuildable_demand`).

Reachable sequence, driven entirely through the real services:
Order B (6) is partly covered by 3 units of existing Surplus, leaving production
demand 3. Run B starts against that requirement (its Surplus allocation is now
active-run-protected). A DIFFERENT run (for Order C's later-date requirement)
finishes, and its Product-wide output completes B's remaining 3 units, so B becomes
READY while Run B is still IN_PRODUCTION."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductionRunStatus, ProductType
from app.db.models.production import (
    IngredientReservation,
    ProductionRequirement,
    ProductionRequirementOrder,
)
from app.db.models.surplus import SurplusAllocation
from app.schemas.inventory import ManualAdjustmentReason
from app.schemas.surplus import SurplusAdjustmentRequest, SurplusManualCreateRequest
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.production_run_service import finish_production, start_production
from app.services.surplus_service import adjust_surplus, create_surplus_manual
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
    make_surplus_inventory,
)

_TODAY = date(2026, 6, 1)


def _allocations(session: Session, **filters):
    session.expire_all()
    return session.scalars(select(SurplusAllocation).filter_by(**filters)).all()


def test_ready_and_active_run_protection_overlap_composes_exactly_once(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session, business, recipe, yield_quantity=Decimal("12"), active_time_minutes=30
    )
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100000")
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )

    order_b = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_b.fulfillment_date = _TODAY
    line_b = make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    order_c = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_c.fulfillment_date = _TODAY + timedelta(days=1)
    line_c = make_order_line(
        session,
        business,
        order_c,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    lot1 = make_surplus_inventory(session, business, product, physical_quantity=Decimal("3"))
    session.flush()
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()

    req_b = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.demand_date == _TODAY)
    ).one()
    req_c = session.scalars(
        select(ProductionRequirement).where(
            ProductionRequirement.demand_date == _TODAY + timedelta(days=1)
        )
    ).one()
    assert req_b.production_demand_quantity == Decimal("3.000000")

    run_b = start_production(
        session, business, req_b.id, batches=None, acknowledged_warning_fingerprint=None
    )
    run_c = start_production(
        session, business, req_c.id, batches=None, acknowledged_warning_fingerprint=None
    )
    session.commit()

    # A DIFFERENT run finishes; its Product-wide output completes B's remaining 3.
    finish_production(
        session,
        business,
        run_c.id,
        expected_version=run_c.version,
        actual_output_quantity=Decimal("3"),
        ingredient_actual_quantities=None,
        business_today=_TODAY,
    )
    session.commit()
    session.expire_all()

    assert session.get(type(order_b), order_b.id).status == OrderStatus.READY
    assert session.get(type(run_b), run_b.id).status == ProductionRunStatus.IN_PRODUCTION

    protected_rows = _allocations(session, order_line_id=line_b.id)
    assert len(protected_rows) == 1
    protected_id = protected_rows[0].id
    assert protected_rows[0].production_requirement_id == req_b.id
    b_reservation_ids = set(
        session.scalars(
            select(IngredientReservation.id).where(
                IngredientReservation.production_requirement_id == req_b.id
            )
        )
    )

    # Ordinary recalculation #1: a new lot arrives (manual creation recalculates the
    # Product). B's protected row must survive untouched; unrelated rebuildable
    # demand (Order C) receives the new lot's availability, not lot1's.
    lot2 = create_surplus_manual(
        session,
        business,
        SurplusManualCreateRequest(
            product_id=product.id,
            quantity=Decimal("2"),
            unit_cost_basis=Decimal("1"),
            is_reusable=True,
        ),
        business_today=_TODAY,
    )
    session.commit()

    def assert_invariants():
        session.expire_all()
        b_rows = _allocations(session, order_line_id=line_b.id)
        assert [r.id for r in b_rows] == [protected_id]  # not duplicated, not cloned
        assert b_rows[0].production_requirement_id == req_b.id
        assert b_rows[0].quantity == Decimal("3.000000")
        assert session.get(ProductionRequirement, req_b.id) is not None
        assert (
            set(
                session.scalars(
                    select(IngredientReservation.id).where(
                        IngredientReservation.production_requirement_id == req_b.id
                    )
                )
            )
            == b_reservation_ids
        )
        linked = session.scalars(
            select(ProductionRequirementOrder.production_requirement_id).where(
                ProductionRequirementOrder.order_line_id == line_b.id
            )
        ).all()
        assert linked == [req_b.id]
        for lot in (lot1, lot2):
            session.refresh(lot)
            total = sum(
                (r.quantity for r in _allocations(session, surplus_inventory_id=lot.id)),
                Decimal(0),
            )
            assert total <= lot.physical_quantity  # never double-counted
        assert session.get(type(order_b), order_b.id).status == OrderStatus.READY

    assert_invariants()
    lot1_rows = _allocations(session, surplus_inventory_id=lot1.id)
    assert [(r.order_line_id, r.quantity) for r in lot1_rows] == [(line_b.id, Decimal("3.000000"))]
    c_rows = _allocations(session, order_line_id=line_c.id)
    assert [(r.surplus_inventory_id, r.quantity) for r in c_rows] == [
        (lot2.id, Decimal("2.000000"))
    ]

    # Ordinary recalculation #2 (direct): still valid and idempotent.
    recalculate_product_closure(session, business, product, business_today=_TODAY)
    session.commit()
    assert_invariants()

    # Hard floor: the active-run axis cannot be released via any reduction of lot1.
    for delta in (Decimal("-1"), Decimal("-3")):
        session.refresh(lot1)
        with pytest.raises(ApiError) as exc_info:
            adjust_surplus(
                session,
                business,
                lot1.id,
                SurplusAdjustmentRequest(
                    version=lot1.version,
                    quantity_delta=delta,
                    reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
                ),
                business_today=_TODAY,
            )
        assert exc_info.value.code == "SURPLUS_ADJUSTMENT_PRODUCTION_LOCKED"
        assert_invariants()

    # Targeted reconciliation on the OTHER lot must leave B's protected row alone.
    session.refresh(lot2)
    with pytest.raises(ApiError) as exc_info:
        adjust_surplus(
            session,
            business,
            lot2.id,
            SurplusAdjustmentRequest(
                version=lot2.version,
                quantity_delta=Decimal("-2"),
                reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            ),
            business_today=_TODAY,
        )
    fingerprint = exc_info.value.issues[0]["details"]["fingerprint"]
    session.refresh(lot2)
    adjust_surplus(
        session,
        business,
        lot2.id,
        SurplusAdjustmentRequest(
            version=lot2.version,
            quantity_delta=Decimal("-2"),
            reason=ManualAdjustmentReason.SPOILAGE_OR_WASTE,
            acknowledged_warning_fingerprint=fingerprint,
        ),
        business_today=_TODAY,
    )
    session.commit()
    assert_invariants()
    assert _allocations(session, order_line_id=line_c.id) == []
