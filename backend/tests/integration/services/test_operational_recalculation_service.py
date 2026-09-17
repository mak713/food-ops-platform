"""Integration tests for `recalculate_product_closure` against a real PostgreSQL test
database (Phase 7 Plan v2 §5/§8, corrected by the Final Pre-Implementation Amendment
and Final Architecture Lock). Exercises the highest-risk behaviors directly against
the service function, bypassing the HTTP/route layer.
"""

from __future__ import annotations

import dataclasses
import decimal
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import MeasurementFamily, OrderLineType, OrderStatus, ProductType
from app.db.models.order import Order
from app.db.models.production import (
    IngredientReservation,
    ProductionRequirement,
    ProductionRequirementOrder,
)
from app.db.models.surplus import SurplusAllocation
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.order_lifecycle_service import confirm_order
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_production_requirement,
    make_production_requirement_order,
    make_production_run,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
    make_surplus_allocation,
    make_surplus_inventory,
)

_TODAY = date(2026, 6, 1)


def _confirmed_order(session: Session, business, *, fulfillment_date: date) -> Order:
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = fulfillment_date
    return order


def _produced_setup(session: Session, *, yield_quantity=12, active_time_minutes=30):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session,
        business,
        recipe,
        yield_quantity=yield_quantity,
        active_time_minutes=active_time_minutes,
    )
    ingredient = make_ingredient(session, business)
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.flush()
    return business, product, recipe, revision, ingredient


def _requirement_for(session: Session, product_id, *, exclude_id=None) -> ProductionRequirement:
    stmt = select(ProductionRequirement).where(ProductionRequirement.product_id == product_id)
    if exclude_id is not None:
        stmt = stmt.where(ProductionRequirement.id != exclude_id)
    return session.scalars(stmt).one()


def test_basic_confirmation_creates_calculated_requirement_and_reservation(session: Session):
    business, product, _recipe, revision, ingredient = _produced_setup(session)
    order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()

    result = recalculate_product_closure(session, business, product, business_today=_TODAY)

    assert result.missing_recipe is False
    requirement = _requirement_for(session, product.id)
    assert requirement.recipe_revision_id == revision.id
    assert requirement.confirmed_demand_quantity == Decimal("6.000000")
    assert requirement.recommended_batches == 1  # ceil(6/12)
    assert requirement.expected_output_quantity == Decimal("12.000000")
    reservation = session.scalars(
        select(IngredientReservation).where(IngredientReservation.ingredient_id == ingredient.id)
    ).one()
    # 500g/batch * 1 batch = 500g required.
    assert reservation.quantity_canonical == Decimal("500.000000")


def test_surplus_fully_covers_demand_zero_production_needed(session: Session):
    business, product, _recipe, _revision, _ingredient = _produced_setup(session)
    order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    make_surplus_inventory(
        session,
        business,
        product,
        physical_quantity=Decimal("10"),
        produced_at=datetime.now(UTC),
    )
    session.flush()

    result = recalculate_product_closure(session, business, product, business_today=_TODAY)

    requirement = _requirement_for(session, product.id)
    assert requirement.surplus_allocated_quantity == Decimal("6.000000")
    assert requirement.production_demand_quantity == Decimal("0.000000")
    assert requirement.recommended_batches == 0
    allocation = session.scalars(
        select(SurplusAllocation).where(SurplusAllocation.order_id == order.id)
    ).one()
    assert allocation.quantity == Decimal("6.000000")
    assert result.missing_recipe is False


def test_missing_recipe_with_full_surplus_coverage_still_reports_missing_recipe(session: Session):
    """Final Pre-Implementation Amendment §10 / Final Architecture Lock: a Produced
    Product with no applicable Recipe stays flagged even when Surplus fully covers
    current demand — the warning must never be suppressed merely because
    production_demand_quantity is zero."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    make_surplus_inventory(
        session, business, product, physical_quantity=Decimal("10"), produced_at=datetime.now(UTC)
    )
    session.flush()

    result = recalculate_product_closure(session, business, product, business_today=_TODAY)

    assert result.missing_recipe is True
    requirement = _requirement_for(session, product.id)
    assert requirement.calculation_status.value == "INCOMPLETE_RECIPE"
    assert requirement.production_demand_quantity == Decimal("0.000000")
    assert requirement.surplus_allocated_quantity == Decimal("6.000000")


def test_wac_first_planned_ingredient_cost(session: Session):
    business, product, _recipe, _revision, ingredient = _produced_setup(session)
    ingredient.physical_quantity = Decimal("50")
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    ingredient.replacement_unit_cost = Decimal("0.09")  # must be ignored: physical_quantity > 0
    order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()

    recalculate_product_closure(session, business, product, business_today=_TODAY)

    requirement = _requirement_for(session, product.id)
    # 500g required * $0.02/g (WAC, not the $0.09 replacement override) = $10.00.
    assert requirement.estimated_ingredient_cost == Decimal("10.000000")


def test_active_run_protection_requirement_survives_recalculation_untouched(session: Session):
    """The invariant flagged mid-implementation: reconciliation must never delete a
    ProductionRequirement referenced by an IN_PRODUCTION run, and a fresh
    recalculation for a *different* Order must not double-allocate Surplus already
    committed to the protected demand."""
    business, product, _recipe, revision, _ingredient = _produced_setup(session)

    protected_order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    protected_line = make_order_line(
        session,
        business,
        protected_order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("12"),
    )
    session.flush()

    protected_requirement = make_production_requirement(
        session,
        business,
        product,
        revision,
        demand_date=_TODAY,
        confirmed_demand_quantity=Decimal("12"),
        production_demand_quantity=Decimal("12"),
    )
    session.flush()
    make_production_requirement_order(
        session, business, protected_requirement, protected_order, protected_line
    )
    surplus_lot = make_surplus_inventory(
        session, business, product, physical_quantity=Decimal("12"), produced_at=datetime.now(UTC)
    )
    session.flush()
    make_surplus_allocation(
        session,
        business,
        surplus_lot,
        protected_requirement,
        protected_order,
        protected_line,
        quantity=Decimal("12"),
    )
    make_production_run(
        session,
        business,
        product,
        revision,
        source_production_requirement_id=protected_requirement.id,
    )
    session.flush()

    protected_requirement_id = protected_requirement.id

    # A second, independent Order confirms new demand for a DIFFERENT date — the
    # same (product, revision, date) key as the protected requirement is correctly
    # rejected outright (the DB's own unique index allows only one row per key, and
    # a protected row may never be re-linked); a different date is the realistic
    # scenario for testing that the two demand groups still correctly compete for
    # the same (date-independent) Surplus lot.
    new_order = _confirmed_order(
        session, business, fulfillment_date=date(_TODAY.year, _TODAY.month, _TODAY.day + 1)
    )
    make_order_line(
        session,
        business,
        new_order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()

    result = recalculate_product_closure(session, business, product, business_today=_TODAY)

    # The protected requirement row still exists, completely unchanged.
    still_there = session.get(ProductionRequirement, protected_requirement_id)
    assert still_there is not None
    assert still_there.confirmed_demand_quantity == Decimal("12")

    # The lot had 12 physical, all 12 already fixed to the protected demand — the
    # new Order's 6 units of demand must find ZERO surplus available, not
    # incorrectly reuse the already-committed 12.
    new_requirement = _requirement_for(session, product.id, exclude_id=protected_requirement_id)
    assert new_requirement.surplus_allocated_quantity == Decimal("0.000000")
    assert new_requirement.production_demand_quantity == Decimal("6.000000")
    assert protected_requirement_id in result.requirement_ids


def test_protected_demand_date_conflict_rejects_new_colliding_confirmation(session: Session):
    """Phase 7 Implementation Remediation Plan, Finding 8: unlike the sibling test
    above (which deliberately uses a DIFFERENT date to exercise Surplus-lot
    competition), this test constructs a new confirmation whose resolved
    `(product, recipe_revision_id, demand_date)` key is EXACTLY the protected
    requirement's own key — the collision `PRODUCTION_LOCKED_DEMAND_DATE_CONFLICT`
    path (operational_recalculation_service.py) was previously undertested; no
    existing test attempted the exact-key collision this asserts."""
    business, product, _recipe, revision, _ingredient = _produced_setup(session)

    protected_order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    protected_line = make_order_line(
        session,
        business,
        protected_order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("12"),
    )
    session.flush()

    protected_requirement = make_production_requirement(
        session,
        business,
        product,
        revision,
        demand_date=_TODAY,
        confirmed_demand_quantity=Decimal("12"),
        production_demand_quantity=Decimal("12"),
    )
    session.flush()
    make_production_requirement_order(
        session, business, protected_requirement, protected_order, protected_line
    )
    make_production_run(
        session,
        business,
        product,
        revision,
        source_production_requirement_id=protected_requirement.id,
    )
    session.flush()

    protected_requirement_id = protected_requirement.id
    # Snapshot every field that must remain byte-for-byte unchanged after the
    # rejected attempt below.
    protected_snapshot = {
        "confirmed_demand_quantity": protected_requirement.confirmed_demand_quantity,
        "production_demand_quantity": protected_requirement.production_demand_quantity,
        "surplus_allocated_quantity": protected_requirement.surplus_allocated_quantity,
        "recipe_revision_id": protected_requirement.recipe_revision_id,
        "demand_date": protected_requirement.demand_date,
    }

    # A second, independent Order confirms new demand for the SAME product,
    # revision (current == the protected requirement's own revision), and date —
    # the exact protected key.
    colliding_order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business,
        colliding_order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()

    # No `session.rollback()` here: the collision check raises before
    # `recalculate_product_closure` ever calls `db.add()`, so there is nothing
    # pending to undo — and this test's own fixture setup above (the protected
    # requirement/order/run) is itself only flushed, not committed, so a rollback
    # here would incorrectly discard it too (matching the sibling test above,
    # which also asserts state directly with no mid-test rollback).
    with pytest.raises(ApiError) as exc_info:
        recalculate_product_closure(session, business, product, business_today=_TODAY)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "PRODUCTION_LOCKED_DEMAND_DATE_CONFLICT"

    # Zero mutation: the protected row is untouched, and no second row/links/
    # reservations were created for the rejected colliding Order.
    still_there = session.get(ProductionRequirement, protected_requirement_id)
    assert still_there is not None
    assert still_there.confirmed_demand_quantity == protected_snapshot["confirmed_demand_quantity"]
    assert (
        still_there.production_demand_quantity == protected_snapshot["production_demand_quantity"]
    )
    assert (
        still_there.surplus_allocated_quantity == protected_snapshot["surplus_allocated_quantity"]
    )
    assert still_there.recipe_revision_id == protected_snapshot["recipe_revision_id"]
    assert still_there.demand_date == protected_snapshot["demand_date"]

    all_requirements_for_product = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).all()
    assert [r.id for r in all_requirements_for_product] == [protected_requirement_id]

    links_for_colliding_line = session.scalars(
        select(ProductionRequirementOrder).where(
            ProductionRequirementOrder.order_id == colliding_order.id
        )
    ).all()
    assert links_for_colliding_line == []

    # The original run's link to the protected requirement is still intact.
    still_protected_requirement_id = session.get(ProductionRequirement, protected_requirement_id).id
    assert still_protected_requirement_id == protected_requirement_id


# --- Phase 7 Implementation Remediation Plan, Finding 4: suggested_start_at ------------


def test_suggested_start_at_persisted_when_fulfillment_time_known(session: Session):
    """Business timezone is America/New_York (factory default); 2026-06-01 is EDT
    (UTC-4). fulfillment_time=14:00 local -> 18:00 UTC deadline; 1 batch *
    elapsed_time_minutes=45 -> suggested_start_at = 17:15 UTC."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session,
        business,
        recipe,
        yield_quantity=Decimal("12"),
        active_time_minutes=30,
    )
    # `make_recipe_revision`'s overrides do not include `elapsed_time_minutes` — set
    # it directly on the ORM object instead.
    revision.elapsed_time_minutes = 45
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(session, business, revision, ingredient, quantity=Decimal("1"))

    order = _confirmed_order(session, business, fulfillment_date=_TODAY)
    order.fulfillment_time = datetime_time(14, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()

    recalculate_product_closure(session, business, product, business_today=_TODAY)

    requirement = _requirement_for(session, product.id)
    assert requirement.recommended_batches == 1
    assert requirement.suggested_start_at is not None
    expected = datetime(2026, 6, 1, 17, 15, tzinfo=UTC)
    assert requirement.suggested_start_at == expected


def test_suggested_start_at_null_when_any_contributing_order_missing_fulfillment_time(
    session: Session,
):
    """If ANY contributing OrderLine's Order lacks a fulfillment_time, the group's
    exact aggregate deadline is insufficiently known -> suggested_start_at stays
    NULL, even though a different contributing Order on the same date DOES have a
    time set."""
    business, product, _recipe, revision, _ingredient = _produced_setup(session, yield_quantity=100)
    # Give the revision an elapsed time so a missing suggested_start_at can only be
    # attributed to the missing fulfillment_time, not a missing elapsed duration.
    revision.elapsed_time_minutes = 45

    order_with_time = _confirmed_order(session, business, fulfillment_date=_TODAY)
    order_with_time.fulfillment_time = datetime_time(14, 0)
    make_order_line(
        session,
        business,
        order_with_time,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("3"),
    )
    order_without_time = _confirmed_order(session, business, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business,
        order_without_time,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("3"),
    )
    session.flush()

    recalculate_product_closure(session, business, product, business_today=_TODAY)

    requirement = _requirement_for(session, product.id)
    assert requirement.confirmed_demand_quantity == Decimal("6.000000")
    assert requirement.suggested_start_at is None


# --- Phase 7 Final Remediation Correction Plan, Finding 7: adversarial tenant- ---------
# scoping coverage for the `business_id` predicates hardened in the prior pass, using
# real foreign-tenant UUIDs across two genuinely independent businesses — never an
# impossible same-primary-key-in-two-businesses fixture (UUID PKs are globally
# unique, so that construction can never occur in real data; see the Round 1
# amendment this project's plan history already settled on that point).


def test_recalculation_never_leaks_a_second_tenants_similarly_shaped_demand(session: Session):
    """Two independent, real Businesses each confirm demand for their OWN Product
    against their OWN Ingredient, deliberately shaped to collide on every
    non-identity dimension a leaking join could plausibly key off: identical
    demand quantity (60), identical physical stock (100), identical fulfillment
    date, and identically-named Ingredient/Product ("Flour"/"Test Product", the
    factories' own shared defaults) — everything EXCEPT the actual row ids
    differs. If `_gather_produced_demand`'s `Order.business_id` filter, or the
    Ingredient-shortage read's own `business_id` predicates, were ever silently
    dropped, Business B's recalculation would see Business A's already-committed
    60-unit reservation as additional "external" demand against B's own
    100-unit stock, incorrectly reporting a shortage that doesn't exist for B in
    isolation. It cannot, because every row is tenant-scoped by its own globally-
    unique id regardless of how similarly the two businesses' data is shaped."""
    business_a = make_business_graph(session)
    product_a = make_product(session, business_a, product_type=ProductType.PRODUCED)
    recipe_a = make_recipe(session, business_a, product_a)
    revision_a = make_recipe_revision(session, business_a, recipe_a, yield_quantity=Decimal("1"))
    ingredient_a = make_ingredient(session, business_a)
    ingredient_a.physical_quantity = Decimal("100")
    make_recipe_revision_ingredient(
        session, business_a, revision_a, ingredient_a, quantity=Decimal("1")
    )
    order_a = _confirmed_order(session, business_a, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business_a,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_a,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    recalculate_product_closure(session, business_a, product_a, business_today=_TODAY)
    session.flush()

    business_b = make_business_graph(session)
    product_b = make_product(session, business_b, product_type=ProductType.PRODUCED)
    recipe_b = make_recipe(session, business_b, product_b)
    revision_b = make_recipe_revision(session, business_b, recipe_b, yield_quantity=Decimal("1"))
    ingredient_b = make_ingredient(session, business_b)
    ingredient_b.physical_quantity = Decimal("100")
    make_recipe_revision_ingredient(
        session, business_b, revision_b, ingredient_b, quantity=Decimal("1")
    )
    order_b = _confirmed_order(session, business_b, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business_b,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_b,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    result_b = recalculate_product_closure(session, business_b, product_b, business_today=_TODAY)

    # Business B's own recalculation sees ONLY its own 60-unit demand against its
    # own 100-unit stock — zero shortage, never contaminated by Business A's
    # already-committed, identically-shaped 60-unit reservation.
    assert len(result_b.ingredient_shortages) == 1
    shortage_b = result_b.ingredient_shortages[0]
    assert shortage_b.ingredient_id == ingredient_b.id
    assert shortage_b.shortage_quantity == Decimal("0")
    assert shortage_b.physical_quantity == Decimal("100")

    requirement_b = _requirement_for(session, product_b.id)
    assert requirement_b.confirmed_demand_quantity == Decimal("60.000000")

    # Business A's own state is completely untouched by Business B's later pass.
    requirement_a = _requirement_for(session, product_a.id)
    assert requirement_a.confirmed_demand_quantity == Decimal("60.000000")
    reservations_a = session.scalars(
        select(IngredientReservation).where(IngredientReservation.ingredient_id == ingredient_a.id)
    ).all()
    assert len(reservations_a) == 1
    assert reservations_a[0].quantity_canonical == Decimal("60.000000")


# --- Manual Acceptance UX Correction Plan, Finding 1/2: IngredientShortageResult -----
# metadata enrichment (name/canonical_unit) and its tenant-scoping.


def test_ingredient_shortage_result_carries_correct_name_and_unit_and_never_leaks_across_tenants(
    session: Session,
):
    """Each `IngredientShortageResult` now carries `ingredient_name`/`canonical_unit`
    alongside `physical_quantity`/`shortage_quantity` (Manual Acceptance UX
    Correction Plan, Finding 1) — enriched at the two existing construction sites
    from an Ingredient row already loaded there, at zero extra queries. Uses two
    DISTINCTLY named/unit'd Ingredients across two independent Businesses so a
    cross-tenant leak (Business B's result carrying Business A's name/unit) would
    be immediately visible, unlike the prior identically-shaped-fixture test above."""
    business_a = make_business_graph(session)
    product_a = make_product(session, business_a, product_type=ProductType.PRODUCED)
    recipe_a = make_recipe(session, business_a, product_a)
    revision_a = make_recipe_revision(session, business_a, recipe_a, yield_quantity=Decimal("1"))
    ingredient_a = make_ingredient(
        session, business_a, name="Business A Cocoa Powder", canonical_unit="g"
    )
    ingredient_a.physical_quantity = Decimal("10")
    make_recipe_revision_ingredient(
        session, business_a, revision_a, ingredient_a, quantity=Decimal("1"), unit="g"
    )
    order_a = _confirmed_order(session, business_a, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business_a,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_a,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    recalculate_product_closure(session, business_a, product_a, business_today=_TODAY)
    session.flush()

    business_b = make_business_graph(session)
    product_b = make_product(session, business_b, product_type=ProductType.PRODUCED)
    recipe_b = make_recipe(session, business_b, product_b)
    revision_b = make_recipe_revision(session, business_b, recipe_b, yield_quantity=Decimal("1"))
    ingredient_b = make_ingredient(
        session,
        business_b,
        name="Business B Vanilla Extract",
        canonical_unit="mL",
        measurement_family=MeasurementFamily.VOLUME,
    )
    ingredient_b.physical_quantity = Decimal("5")
    make_recipe_revision_ingredient(
        session, business_b, revision_b, ingredient_b, quantity=Decimal("1"), unit="mL"
    )
    order_b = _confirmed_order(session, business_b, fulfillment_date=_TODAY)
    make_order_line(
        session,
        business_b,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_b,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    result_b = recalculate_product_closure(session, business_b, product_b, business_today=_TODAY)

    assert len(result_b.ingredient_shortages) == 1
    shortage_b = result_b.ingredient_shortages[0]
    assert shortage_b.ingredient_id == ingredient_b.id
    assert shortage_b.ingredient_name == "Business B Vanilla Extract"
    assert shortage_b.canonical_unit == "mL"
    assert shortage_b.physical_quantity == Decimal("5")
    # Never Business A's name/unit, despite both recalculations running in the
    # same test session against otherwise identically-shaped demand/quantities.
    assert shortage_b.ingredient_name != "Business A Cocoa Powder"
    assert shortage_b.canonical_unit != "g"


# --- Phase 7 Final Remediation Correction Plan, Finding 7: service-level -------------
# representability-overflow coverage through the REAL recalculation path (a real
# `confirm_order` call), not merely the existing pure-function boundary tests for
# `is_representable_in_int32`/`is_representable_in_numeric_18_6` themselves.


def test_tiny_yield_with_maximal_demand_overflows_recommended_batches_via_real_path(
    session: Session,
):
    """A tiny Recipe yield combined with a maximal-but-individually-valid demand
    quantity legitimately drives `recommended_batches` past PostgreSQL's signed
    32-bit INTEGER range through the REAL `confirm_order` workflow — proving the
    real Confirm path rejects it (zero mutation), not merely that
    `is_representable_in_int32`/`_persist_int32` behave correctly in isolation."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("0.000001"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("999999999999.999999")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("0.000001")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = datetime_time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("999999999999.999999"),  # NUMERIC(18,6) max, itself valid
    )
    session.flush()
    session.commit()

    raised = None
    try:
        confirm_order(
            session,
            business,
            order.id,
            expected_version=order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "PRODUCTION_REQUIREMENT_VALUE_OVERFLOW"

    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )
    reloaded = session.get(Order, order.id)
    assert reloaded.status == OrderStatus.DRAFT
    assert reloaded.confirmed_at is None


def test_tiny_ingredient_requirement_below_storage_quantum_rejected_via_real_confirm(
    session: Session,
):
    """Phase 7 Final Semantic & Precision Correction Plan, Finding 2 — an
    Ingredient's `canonical_unit` is a free per-Ingredient choice within its
    measurement family, so a Recipe line specified in a much smaller unit ("g")
    against an Ingredient canonicalized in a much larger one ("kg") can produce a
    genuinely positive raw requirement that rounds to `0.000000` once converted
    and quantized to the stored NUMERIC(18,6) precision. The real `confirm_order`
    workflow must reject this with a structured `QUANTITY_TOO_SMALL` domain error
    — never a raw DB CHECK-constraint `IntegrityError` — and mutate nothing."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business, canonical_unit="kg")
    ingredient.physical_quantity = Decimal("10000")
    # 0.000001 g per batch, converted to the Ingredient's own "kg" canonical unit,
    # is 0.000000001 kg -- genuinely positive but below the 6dp storage quantum.
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("0.000001"), unit="g"
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = datetime_time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),  # ceil(6/12) == 1 batch
    )
    session.flush()
    session.commit()

    raised = None
    try:
        confirm_order(
            session,
            business,
            order.id,
            expected_version=order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "QUANTITY_TOO_SMALL"

    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )
    assert (
        session.scalars(
            select(IngredientReservation).where(
                IngredientReservation.ingredient_id == ingredient.id
            )
        ).first()
        is None
    )
    reloaded = session.get(Order, order.id)
    assert reloaded.status == OrderStatus.DRAFT
    assert reloaded.confirmed_at is None


def test_large_ingredient_quantity_and_batches_overflow_required_quantity_canonical(
    session: Session,
):
    """A derived quantity (`quantity_per_batch * batches` -> the Ingredient's own
    canonical-unit requirement) can legitimately overflow `NUMERIC(18,6)` even
    though EITHER factor alone, and even `recommended_batches` itself, stays well
    within its own individual bound (`batches` here is a modest 2,000,000, far
    under the INTEGER range) — this isolates the `required_quantity_canonical`
    persistence check specifically, distinct from the `recommended_batches`
    overflow the sibling test above proves, through the real `confirm_order`
    workflow."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    # yield=1 -> recommended_batches == the demand quantity itself.
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("1"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("999999999999.999999")
    # 600,000 units/batch * 2,000,000 batches = 1.2e12, past the 999999999999.999999
    # NUMERIC(18,6) ceiling — while 2,000,000 batches is nowhere near INT32_MAX.
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("600000")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = datetime_time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("2000000"),
    )
    session.flush()
    session.commit()

    raised = None
    try:
        confirm_order(
            session,
            business,
            order.id,
            expected_version=order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "PRODUCTION_REQUIREMENT_VALUE_OVERFLOW"

    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )
    reloaded = session.get(Order, order.id)
    assert reloaded.status == OrderStatus.DRAFT
    assert reloaded.confirmed_at is None


def test_recalculate_product_closure_unaffected_by_corrupted_ambient_decimal_context(
    session: Session,
):
    """ADR-109, service-layer coverage (authorized alongside the Phase 7 Final
    Semantic & Precision Correction Plan) — `recalculate_product_closure`'s own
    `decimal.localcontext()` sites (including the per-Ingredient quantization/
    summation loop Findings 2/3 introduced) must be genuinely isolated from the
    caller's ambient global Decimal context, never merely coincidentally correct
    because no test happens to corrupt it. Mirrors the existing domain-layer
    ADR-109 regression test (`test_surplus_allocation.
    test_allocation_result_unaffected_by_corrupted_ambient_decimal_context`):
    deliberately corrupt the global context (very low precision, a non-default
    rounding mode) immediately before calling, and assert the result is
    bit-for-bit identical to the same call under a pristine ambient context."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("7"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100000")
    ingredient.weighted_average_unit_cost = Decimal("0.037")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("333")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = datetime_time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("20"),  # ceil(20/7) == 3 batches
    )
    session.flush()
    confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    session.commit()

    baseline = recalculate_product_closure(
        session,
        business,
        product,
        business_today=_TODAY,
        exclude_order_id=None,
        hypothetical_lines=(),
        persist=False,
    )

    original_ctx = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 2
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        corrupted = recalculate_product_closure(
            session,
            business,
            product,
            business_today=_TODAY,
            exclude_order_id=None,
            hypothetical_lines=(),
            persist=False,
        )
    finally:
        decimal.setcontext(original_ctx)

    # `requirement_id` is a freshly-generated uuid4() for each unpersisted
    # (`persist=False`) rebuild -- not deterministic across two separate calls
    # by design, and irrelevant to this ADR-109 arithmetic-isolation proof.
    # Every OTHER field must be bit-for-bit identical.
    assert len(baseline.requirements) == 1
    assert len(corrupted.requirements) == 1
    baseline_item = dataclasses.replace(baseline.requirements[0], requirement_id=None)
    corrupted_item = dataclasses.replace(corrupted.requirements[0], requirement_id=None)
    assert baseline_item == corrupted_item
    assert baseline.candidate_ingredient_totals == corrupted.candidate_ingredient_totals
    assert baseline.ingredient_shortages == corrupted.ingredient_shortages
    assert baseline.requirements[0].estimated_ingredient_cost is not None
