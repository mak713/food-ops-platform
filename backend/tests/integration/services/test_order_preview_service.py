"""Integration tests for `order_preview_service.preview_order` (Phase 7 Plan v2 §12,
corrected by the Final Pre-Implementation Amendment §16 and the Phase 7 Implementation
Remediation Plan, Finding 2/amendment 2)."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.production import IngredientReservation, ProductionRequirement
from app.schemas.order import OrderConfirmedEditRequest, OrderCreateRequest, OrderLineInput
from app.services.order_lifecycle_service import confirm_order, update_confirmed_order
from app.services.order_preview_service import preview_order
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
)

_TODAY = date(2026, 6, 1)


def test_preview_computes_totals_and_operational_result_with_zero_writes(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    ingredient.weighted_average_unit_cost = Decimal("0.01")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("30.00"),
            )
        ],
    )

    result = preview_order(session, business, payload, business_today=_TODAY)

    assert result.final_total == Decimal("30.00")
    assert len(result.requirement_previews) == 1
    item = result.requirement_previews[0]
    assert item.missing_recipe is False
    # A brand-new Order: baseline is zero (nothing existed before it), projected
    # equals this Order's own hypothetical contribution, incremental is identical.
    assert item.baseline.confirmed_demand_quantity == Decimal("0.000000")
    assert item.projected.confirmed_demand_quantity == Decimal("6.000000")
    assert item.projected.recipe_revision_id == revision.id

    # Zero-write guarantee.
    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )


def test_confirmed_order_preview_substitution_never_double_counts_own_demand(session: Session):
    """Final Pre-Implementation Amendment §4 / Remediation Plan Finding 2, amendment
    2: previewing a proposed edit to an already-CONFIRMED Order must present
    `confirmed world - this Order's own persisted contribution + hypothetical
    replacement` — never the whole closure as though the edit itself caused the
    OTHER Order's already-existing demand. Baseline must reflect only the sibling
    Order's 4 units; projected must reflect 4 + the proposed 10; incremental must
    reflect only this Order's own +10 contribution, never 16."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("100"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100000")
    ingredient.weighted_average_unit_cost = Decimal("0.01")
    make_recipe_revision_ingredient(session, business, revision, ingredient, quantity=Decimal("1"))
    session.flush()

    # Sibling Order B: 4 units, confirmed, untouched by this preview.
    order_b = make_order(session, business, status=OrderStatus.DRAFT)
    order_b.fulfillment_date = _TODAY
    order_b.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("4"),
    )
    session.flush()
    confirmed_b, _ = confirm_order(
        session,
        business,
        order_b.id,
        expected_version=order_b.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    session.commit()

    # Order A: 6 units, confirmed — the Order being previewed for an edit.
    order_a = make_order(session, business, status=OrderStatus.DRAFT)
    order_a.fulfillment_date = _TODAY
    order_a.fulfillment_time = time(9, 0)
    line_a = make_order_line(
        session,
        business,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    confirmed_a, _ = confirm_order(
        session,
        business,
        order_a.id,
        expected_version=order_a.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    session.commit()

    original_pin = session.scalars(
        select(ProductionRequirement.recipe_revision_id).where(
            ProductionRequirement.product_id == product.id
        )
    ).first()
    assert original_pin == revision.id

    # Preview: propose raising Order A's own line to 10 units (same product,
    # same stable line id — retained, not a new line).
    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                id=line_a.id,
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("10"),
                charged_unit_price=Decimal("30.00"),
            )
        ],
    )

    result = preview_order(
        session, business, payload, order_id=confirmed_a.id, business_today=_TODAY
    )

    assert len(result.requirement_previews) == 1
    item = result.requirement_previews[0]
    assert item.baseline.confirmed_demand_quantity == Decimal("4.000000")  # only Order B
    assert item.projected.confirmed_demand_quantity == Decimal("14.000000")  # B(4) + A(10)
    incremental = item.projected.confirmed_demand_quantity - item.baseline.confirmed_demand_quantity
    assert incremental == Decimal("10.000000")  # this Order's own contribution, never 16
    # Historical pin preserved for the same-product retained line (Amendment §4).
    assert item.projected.recipe_revision_id == original_pin

    # Zero-write guarantee: Order A's real persisted requirement is untouched.
    session.expire_all()
    real_requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert real_requirement.confirmed_demand_quantity == Decimal("10.000000")  # A(6)+B(4)


# --- Phase 7 Final Remediation Correction Plan, Finding 5: multi-Product shared- -------
# Ingredient combined candidate-state calculation.


def test_preview_combined_shortage_across_shared_ingredient_products(session: Session):
    """Two Products sharing one Ingredient, each independently under its own
    physical-stock threshold (60 < 100), but jointly exceeding it (120 > 100, a
    real shortage of 20). Each Product's own independent, zero-write
    `recalculate_product_closure(persist=False)` call in `preview_order`'s loop
    can only ever see its OWN hypothetical candidate — proves the combined read
    reports 20, never 0 (the naive per-product accumulation this correction
    replaces would silently report 0, since 60 < 100 on each Product's own)."""
    business = make_business_graph(session)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("100")
    ingredient.weighted_average_unit_cost = Decimal("0.01")
    session.flush()

    product_a = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe_a = make_recipe(session, business, product_a)
    revision_a = make_recipe_revision(session, business, recipe_a, yield_quantity=Decimal("1"))
    make_recipe_revision_ingredient(
        session, business, revision_a, ingredient, quantity=Decimal("1")
    )

    product_b = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe_b = make_recipe(session, business, product_b)
    revision_b = make_recipe_revision(session, business, recipe_b, yield_quantity=Decimal("1"))
    make_recipe_revision_ingredient(
        session, business, revision_b, ingredient, quantity=Decimal("1")
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product_a.id,
                underlying_quantity=Decimal("60"),
                charged_unit_price=Decimal("1.00"),
            ),
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product_b.id,
                underlying_quantity=Decimal("60"),
                charged_unit_price=Decimal("1.00"),
            ),
        ],
    )

    result = preview_order(session, business, payload, business_today=_TODAY)

    availability_by_id = {a.ingredient_id: a for a in result.ingredient_availability}
    assert ingredient.id in availability_by_id
    entry = availability_by_id[ingredient.id]
    assert entry.baseline_shortage_quantity == Decimal("0")
    assert entry.projected_shortage_quantity == Decimal("20")  # 60+60-100, never 0
    # Manual Acceptance UX Correction Plan, Finding 1 — the Preview result now
    # identifies the Ingredient by name/unit, not only its opaque id.
    assert entry.ingredient_name == ingredient.name
    assert entry.canonical_unit == ingredient.canonical_unit

    # The combined shortage also drives the warning-acknowledgement protocol.
    assert any(
        issue["code"] == "INGREDIENT_SHORTAGE"
        and issue["details"]["ingredient_id"] == str(ingredient.id)
        for issue in result.warning_issues
    )
    matching_fingerprints = [
        f
        for f in result.warning_fingerprints
        if f.startswith(f"INGREDIENT_SHORTAGE:{ingredient.id}:")
    ]
    assert len(matching_fingerprints) == 1  # exactly one, never two divergent ones

    # Zero-write guarantee.
    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product_a.id)
        ).first()
        is None
    )


# --- Phase 7 Final Semantic & Precision Correction Plan, Finding 2: Preview must --------
# reject a positive-derived-quantity-that-rounds-to-zero the same way the real Confirm
# path does.


def test_tiny_ingredient_requirement_below_storage_quantum_rejected_via_preview(
    session: Session,
):
    """Preview's zero-write path must reject the identical positive-quantity-
    collapses-to-zero scenario `recalculate_product_closure` rejects via the real
    Confirm path (see `test_operational_recalculation_service.
    test_tiny_ingredient_requirement_below_storage_quantum_rejected_via_real_confirm`),
    for BOTH a brand-new unsaved Order and an edit to a persisted DRAFT Order."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business, canonical_unit="kg")
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("0.000001"), unit="g"
    )
    session.commit()

    new_order_payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )
    raised = None
    try:
        preview_order(session, business, new_order_payload, business_today=_TODAY)
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

    # Persisted-Draft-edit variant.
    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.commit()

    draft_edit_payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                id=line.id,
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )
    raised = None
    try:
        preview_order(
            session, business, draft_edit_payload, order_id=order.id, business_today=_TODAY
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
    assert order.status == OrderStatus.DRAFT


# --- Phase 7 Final Semantic & Precision Correction Plan, Finding 3: Preview's -----------
# candidate reservations must use the same would-be-persisted (quantized) quantities a
# real Commit would write.


def test_preview_candidate_ingredient_totals_match_would_be_persisted_quantities(
    session: Session,
):
    """A canonical_unit="oz"/recipe-unit="g" pairing makes the rounding boundary
    observable: 13g converted to oz is genuinely 0.4585620974... -- quantized to
    0.458562. Preview's reported shortage must reflect the QUANTIZED value (a
    clean 6dp figure), never the raw one (which would produce a shortage with
    more than 6 significant decimal digits)."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business, canonical_unit="oz")
    ingredient.physical_quantity = Decimal("0.4")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("13"), unit="g"
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                id=line.id,
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )
    result = preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)

    availability_by_id = {a.ingredient_id: a for a in result.ingredient_availability}
    entry = availability_by_id[ingredient.id]
    # 0.458562 (quantized) - 0.4 == 0.058562 exactly; the raw, unrounded value
    # (0.4585620974...) minus 0.4 would NOT equal a clean 6dp figure.
    assert entry.projected_shortage_quantity == Decimal("0.058562")

    # The real Confirm immediately following persists the identical quantized
    # amount used in Preview's own shortage math.
    confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=result.warning_fingerprints,
        business_today=_TODAY,
    )
    session.expire_all()
    reservation = session.scalars(
        select(IngredientReservation).where(IngredientReservation.ingredient_id == ingredient.id)
    ).one()
    assert reservation.quantity_canonical == Decimal("0.458562")


def test_preview_multi_group_individual_rounding_differs_from_aggregate_rounding(
    session: Session,
):
    """Two Products sharing one Ingredient, each contributing its own per-group
    canonical requirement whose INDIVIDUAL rounding differs from what rounding
    their combined raw total once would produce (1g and 13g at canonical_unit
    "oz": individually-rounded-then-summed is 0.493836; rounding the raw sum
    once would instead give 0.493835) — Preview's combined shortage must match
    the real, round-per-group-then-sum persisted world, never a rounded-once
    aggregate."""
    business = make_business_graph(session)
    ingredient = make_ingredient(session, business, canonical_unit="oz")
    ingredient.physical_quantity = Decimal("0.4")
    session.flush()

    product_a = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe_a = make_recipe(session, business, product_a)
    revision_a = make_recipe_revision(session, business, recipe_a, yield_quantity=Decimal("12"))
    make_recipe_revision_ingredient(
        session, business, revision_a, ingredient, quantity=Decimal("1"), unit="g"
    )

    product_b = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe_b = make_recipe(session, business, product_b)
    revision_b = make_recipe_revision(session, business, recipe_b, yield_quantity=Decimal("12"))
    make_recipe_revision_ingredient(
        session, business, revision_b, ingredient, quantity=Decimal("13"), unit="g"
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product_a.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("1.00"),
            ),
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product_b.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("1.00"),
            ),
        ],
    )

    result = preview_order(session, business, payload, business_today=_TODAY)

    availability_by_id = {a.ingredient_id: a for a in result.ingredient_availability}
    entry = availability_by_id[ingredient.id]
    assert entry.projected_shortage_quantity == Decimal("0.093836")


def test_preview_warning_fingerprint_matches_immediately_following_real_confirm(
    session: Session,
):
    """The exact fingerprint Preview reports for an Ingredient shortage must be
    the SAME fingerprint the real Confirm recomputes fresh under lock
    immediately afterward — confirming while acknowledging only Preview's own
    reported fingerprint(s) must succeed; confirming with nothing acknowledged
    must still be rejected (proving the shortage is real, not a false
    positive/negative mismatch between the two paths)."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business, canonical_unit="oz")
    ingredient.physical_quantity = Decimal("0.4")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("13"), unit="g"
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.commit()
    expected_version = order.version

    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                id=line.id,
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )
    result = preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    assert any(
        f.startswith(f"INGREDIENT_SHORTAGE:{ingredient.id}:") for f in result.warning_fingerprints
    )

    # Confirming with NOTHING acknowledged is rejected -- the shortage is real.
    raised = None
    try:
        confirm_order(
            session,
            business,
            order.id,
            expected_version=expected_version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc
    assert raised is not None
    assert raised.code == "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
    session.expire_all()
    assert order.status == OrderStatus.DRAFT

    # Confirming with EXACTLY Preview's own reported fingerprint(s) succeeds --
    # the real, freshly-recomputed fingerprint is byte-for-byte identical.
    confirmed, _results = confirm_order(
        session,
        business,
        order.id,
        expected_version=expected_version,
        acknowledged_warning_fingerprints=result.warning_fingerprints,
        business_today=_TODAY,
    )
    assert confirmed.status == OrderStatus.CONFIRMED


# --- Phase 7 Final Semantic & Precision Correction Plan, Finding 4: CONFIRMED Preview ---
# must not re-inject a retained active-run-protected line as hypothetical rebuildable
# demand.


def _confirmed_order_with_protected_line(session: Session):
    """A real CONFIRMED Order with one CUSTOM_QUANTITY line covered by an active
    IN_PRODUCTION run, plus a second, unrelated Product with no Recipe (used only
    to prove a source-change is rejected before any recalculation is attempted)."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    other_product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )

    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    protected_line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
        charged_unit_price_snapshot=Decimal("10.00"),
        line_subtotal=Decimal("10.00"),
    )
    session.flush()

    requirement = make_production_requirement(
        session,
        business,
        product,
        revision,
        demand_date=_TODAY,
        confirmed_demand_quantity=Decimal("6"),
        production_demand_quantity=Decimal("6"),
    )
    session.flush()
    make_production_requirement_order(session, business, requirement, order, protected_line)
    make_production_run(
        session, business, product, revision, source_production_requirement_id=requirement.id
    )
    session.commit()
    return business, product, other_product, order, protected_line, requirement


def _protected_line_payload(order, protected_line, product, **line_overrides) -> OrderCreateRequest:
    defaults: dict = dict(
        id=protected_line.id,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product_id=product.id,
        underlying_quantity=Decimal("6"),
        charged_unit_price=Decimal("10.00"),
    )
    defaults.update(line_overrides)
    return OrderCreateRequest(
        fulfillment_date=order.fulfillment_date,
        fulfillment_time=order.fulfillment_time,
        lines=[OrderLineInput(**defaults)],
    )


def test_confirmed_preview_production_locked_price_only_edit_succeeds_and_preserves_protected_state(
    session: Session,
):
    business, product, _other, order, _protected_line, requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = _protected_line_payload(
        order, _protected_line, product, charged_unit_price=Decimal("15.00")
    )

    result = preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)

    assert len(result.requirement_previews) == 1
    item = result.requirement_previews[0]
    assert item.is_protected is True
    assert item.baseline.confirmed_demand_quantity == Decimal("6.000000")
    assert item.projected.confirmed_demand_quantity == Decimal("6.000000")
    assert item.projected.recipe_revision_id == item.baseline.recipe_revision_id
    assert result.subtotal == Decimal("15.00")

    session.expire_all()
    real_requirement = session.get(ProductionRequirement, requirement.id)
    assert real_requirement.confirmed_demand_quantity == Decimal("6.000000")


def test_confirmed_preview_production_locked_notes_only_edit_succeeds(session: Session):
    business, product, _other, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = _protected_line_payload(order, protected_line, product, notes="Handle with care")

    result = preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)

    assert len(result.requirement_previews) == 1
    item = result.requirement_previews[0]
    assert item.is_protected is True
    assert item.projected.confirmed_demand_quantity == Decimal("6.000000")


def test_confirmed_preview_production_locked_quantity_change_returns_order_production_locked(
    session: Session,
):
    business, product, _other, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = _protected_line_payload(
        order, protected_line, product, underlying_quantity=Decimal("9")
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_PRODUCTION_LOCKED"


def test_confirmed_preview_production_locked_source_change_returns_order_production_locked(
    session: Session,
):
    business, product, other_product, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = _protected_line_payload(order, protected_line, other_product)

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_PRODUCTION_LOCKED"


def test_confirmed_preview_production_locked_date_time_change_returns_order_production_locked(
    session: Session,
):
    business, product, _other, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = OrderCreateRequest(
        fulfillment_date=date(2026, 6, 2),  # changed from the Order's own stored date
        fulfillment_time=order.fulfillment_time,
        lines=[
            OrderLineInput(
                id=protected_line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_PRODUCTION_LOCKED"


def test_confirmed_preview_and_real_edit_parity_for_protected_lines(session: Session):
    """For both the price-only (succeeds) and quantity-change (rejected) cases,
    Preview and an immediately-following real confirmed edit must reach the
    IDENTICAL outcome."""
    business, product, _other, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )

    price_only_payload = _protected_line_payload(
        order, protected_line, product, charged_unit_price=Decimal("15.00")
    )
    preview_order(session, business, price_only_payload, order_id=order.id, business_today=_TODAY)

    price_only_edit = OrderConfirmedEditRequest(
        version=order.version,
        customer_id=order.customer_id,
        fulfillment_date=order.fulfillment_date,
        fulfillment_time=order.fulfillment_time,
        lines=price_only_payload.lines,
        acknowledged_warning_fingerprints=[],
    )
    update_confirmed_order(
        session,
        business,
        order.id,
        price_only_edit,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    # -- A second, independent fixture for the quantity-change (rejected) case. --
    business2, product2, _other2, order2, protected_line2, _requirement2 = (
        _confirmed_order_with_protected_line(session)
    )
    qty_payload = _protected_line_payload(
        order2, protected_line2, product2, underlying_quantity=Decimal("9")
    )
    preview_raised = None
    try:
        preview_order(session, business2, qty_payload, order_id=order2.id, business_today=_TODAY)
    except ApiError as exc:
        preview_raised = exc
    assert preview_raised is not None
    assert preview_raised.code == "ORDER_PRODUCTION_LOCKED"

    real_edit = OrderConfirmedEditRequest(
        version=order2.version,
        customer_id=order2.customer_id,
        fulfillment_date=order2.fulfillment_date,
        fulfillment_time=order2.fulfillment_time,
        lines=qty_payload.lines,
        acknowledged_warning_fingerprints=[],
    )
    real_raised = None
    try:
        update_confirmed_order(
            session,
            business2,
            order2.id,
            real_edit,
            expected_version=order2.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        real_raised = exc
    assert real_raised is not None
    assert real_raised.code == "ORDER_PRODUCTION_LOCKED"


# --- Phase 7 Final Semantic & Precision Correction Plan, Finding 5: a missing Draft -----
# fulfillment date must never be silently treated as "today".


def test_preview_new_order_without_fulfillment_date_fabricates_no_operational_demand(
    session: Session,
):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=None,
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            ),
            OrderLineInput(
                line_type="CUSTOM_ITEM",
                display_name="Custom cake topper",
                underlying_quantity=Decimal("2"),
                charged_unit_price=Decimal("5.00"),
                custom_active_time_minutes=15,
            ),
        ],
    )

    result = preview_order(session, business, payload, business_today=_TODAY)

    assert result.fulfillment_date_required_for_operational_preview is True
    assert result.requirement_previews == []
    assert result.custom_item_workload == []
    assert result.subtotal == Decimal("20.00")
    assert result.final_total == Decimal("20.00")

    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )


def test_preview_persisted_draft_without_fulfillment_date_fabricates_no_operational_demand(
    session: Session,
):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    # Deliberately no fulfillment_date/time set on the persisted Draft itself.
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=None,
        lines=[
            OrderLineInput(
                id=line.id,
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            ),
        ],
    )

    result = preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)

    assert result.fulfillment_date_required_for_operational_preview is True
    assert result.requirement_previews == []
    assert result.subtotal == Decimal("10.00")

    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )


def test_preview_with_fulfillment_date_present_computes_operational_demand_normally(
    session: Session,
):
    """Sibling positive-path guard for Finding 5 — a genuinely present
    `fulfillment_date` must still drive full operational demand computation; the
    fix must not disable Preview's operational computation unconditionally."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=_TODAY,
        fulfillment_time=time(9, 0),
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            ),
        ],
    )

    result = preview_order(session, business, payload, business_today=_TODAY)

    assert result.fulfillment_date_required_for_operational_preview is False
    assert len(result.requirement_previews) == 1
    assert result.requirement_previews[0].projected.confirmed_demand_quantity == Decimal("6.000000")


# --- Phase 7 Final Semantic & Precision Correction Plan, Finding 6: Preview must use ----
# non-locking reference resolution.


def test_preview_reference_resolution_issues_no_for_update_lock(session: Session):
    """Preview's entire zero-write call must acquire NO PostgreSQL row locks at
    all, end to end: it always rolls back immediately, so a transient `FOR
    UPDATE` lock would be real but pointless. Covers both a brand-new Order
    (reference resolution only) and a CONFIRMED-Order edit (reference
    resolution AND Finding 4's own new active-run-protection read)."""
    business, product, other_product, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    session.commit()

    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", _record)
    try:
        # A DIFFERENT, unrelated Product (no Recipe, no existing demand) -- the
        # protected Product's own demand_date/revision is already covered by an
        # active production run, so genuinely new demand for IT would correctly
        # collide (a distinct, already-tested precondition); this sub-case exists
        # only to prove reference resolution issues no lock, not to exercise that
        # collision.
        new_order_payload = OrderCreateRequest(
            fulfillment_date=_TODAY,
            fulfillment_time=time(9, 0),
            lines=[
                OrderLineInput(
                    line_type="CUSTOM_QUANTITY",
                    product_id=other_product.id,
                    underlying_quantity=Decimal("6"),
                    charged_unit_price=Decimal("10.00"),
                )
            ],
        )
        preview_order(session, business, new_order_payload, business_today=_TODAY)

        confirmed_edit_payload = _protected_line_payload(
            order, protected_line, product, charged_unit_price=Decimal("20.00")
        )
        preview_order(
            session, business, confirmed_edit_payload, order_id=order.id, business_today=_TODAY
        )
    finally:
        event.remove(connection, "before_cursor_execute", _record)

    lowered = [sql.lower() for sql in statements]
    assert statements, "expected at least some SQL to have been captured"
    assert not any("for update" in sql for sql in lowered), (
        "Preview issued a FOR UPDATE lock; it must be entirely read-only"
    )


# --- Phase 7 Final Lifecycle Invariant Correction Plan, Finding B: CONFIRMED Preview ---
# must use the same structural rule; explicit lifecycle guard for Preview.


def test_preview_ready_order_rejected_as_not_previewable(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.READY)
    session.commit()

    payload = OrderCreateRequest(fulfillment_date=_TODAY, lines=[])

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_PREVIEWABLE"


def test_preview_completed_order_rejected_as_not_previewable(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.COMPLETED)
    session.commit()

    payload = OrderCreateRequest(fulfillment_date=_TODAY, lines=[])

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_PREVIEWABLE"


def test_preview_canceled_order_rejected_as_not_previewable(session: Session):
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.CANCELED)
    session.commit()

    payload = OrderCreateRequest(fulfillment_date=_TODAY, lines=[])

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_PREVIEWABLE"


def _confirmed_order_without_protection(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    confirmed, _results = confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    session.commit()
    return business, product, confirmed


def test_confirmed_preview_empty_lines_returns_order_not_confirmable(session: Session):
    business, product, confirmed = _confirmed_order_without_protection(session)
    payload = OrderCreateRequest(
        fulfillment_date=confirmed.fulfillment_date,
        fulfillment_time=confirmed.fulfillment_time,
        lines=[],
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=confirmed.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"
    assert any(issue["code"] == "ORDER_NO_LINES" for issue in raised.issues)

    session.expire_all()
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("6.000000")


def test_confirmed_preview_missing_fulfillment_date_returns_order_not_confirmable(
    session: Session,
):
    business, product, confirmed = _confirmed_order_without_protection(session)
    line = confirmed.lines[0]
    payload = OrderCreateRequest(
        fulfillment_date=None,
        lines=[
            OrderLineInput(
                id=line.id,
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=confirmed.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"
    assert any(issue["code"] == "ORDER_MISSING_FULFILLMENT_DATE" for issue in raised.issues)


def test_confirmed_preview_structural_check_runs_even_without_protected_lines(session: Session):
    """Regression test for the exact bug: proves the structural check isn't
    accidentally left nested inside `if protected_line_ids:` -- it must still
    fire when there are zero protected lines."""
    business, product, confirmed = _confirmed_order_without_protection(session)
    payload = OrderCreateRequest(
        fulfillment_date=confirmed.fulfillment_date,
        fulfillment_time=confirmed.fulfillment_time,
        lines=[],
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=confirmed.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"


def test_confirmed_preview_and_real_edit_parity_for_structural_violations(session: Session):
    business, product, confirmed = _confirmed_order_without_protection(session)

    empty_lines_payload = OrderCreateRequest(
        fulfillment_date=confirmed.fulfillment_date,
        fulfillment_time=confirmed.fulfillment_time,
        lines=[],
    )
    preview_raised = None
    try:
        preview_order(
            session, business, empty_lines_payload, order_id=confirmed.id, business_today=_TODAY
        )
    except ApiError as exc:
        preview_raised = exc
    assert preview_raised is not None
    assert preview_raised.code == "ORDER_NOT_CONFIRMABLE"

    real_edit_payload = OrderConfirmedEditRequest(
        version=confirmed.version,
        customer_id=confirmed.customer_id,
        fulfillment_date=confirmed.fulfillment_date,
        fulfillment_time=confirmed.fulfillment_time,
        lines=[],
        acknowledged_warning_fingerprints=[],
    )
    real_raised = None
    try:
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            real_edit_payload,
            expected_version=confirmed.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        real_raised = exc
    assert real_raised is not None
    assert real_raised.code == "ORDER_NOT_CONFIRMABLE"


def test_confirmed_preview_structural_violation_wins_over_production_locked(session: Session):
    """A production-locked CONFIRMED Order previewed with a structurally-invalid
    payload must be rejected as `ORDER_NOT_CONFIRMABLE`, never masked by
    `ORDER_PRODUCTION_LOCKED` -- the Preview-side twin of the identical
    real-edit-path test."""
    business, product, _other, order, _protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = OrderCreateRequest(
        fulfillment_date=order.fulfillment_date,
        fulfillment_time=order.fulfillment_time,
        lines=[],
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"


# --- Phase 7 Final Lifecycle Invariant Correction Plan, Finding C: Business-local ------
# DST validity for Preview.


def test_preview_new_order_nonexistent_local_time_rejected(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.commit()

    payload = OrderCreateRequest(
        fulfillment_date=date(2026, 3, 8),
        fulfillment_time=time(2, 30),
        lines=[
            OrderLineInput(
                line_type="CUSTOM_QUANTITY",
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    raised = None
    try:
        preview_order(session, business, payload, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"

    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )


def test_confirmed_preview_ambiguous_local_time_rejected(session: Session):
    business, product, _other, order, protected_line, _requirement = (
        _confirmed_order_with_protected_line(session)
    )
    payload = OrderCreateRequest(
        fulfillment_date=date(2026, 11, 1),
        fulfillment_time=time(1, 30),
        lines=[
            OrderLineInput(
                id=protected_line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    raised = None
    try:
        preview_order(session, business, payload, order_id=order.id, business_today=_TODAY)
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"
