"""Integration tests for `order_lifecycle_service.confirm_order`/`cancel_confirmed_order`
(Phase 7 Plan v2 §6/§11, corrected by the Final Pre-Implementation Amendment and Final
Architecture Lock §A/§F)."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.production import ProductionRequirement
from app.schemas.order import OrderConfirmedEditRequest, OrderLineInput, OrderUpdateRequest
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.order_lifecycle_service import (
    cancel_confirmed_order,
    confirm_order,
    update_confirmed_order,
)
from app.services.order_service import update_draft_order
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
    make_selling_option,
)

_TODAY = date(2026, 6, 1)


def _draft_setup(session: Session, *, ingredient_physical_quantity=Decimal("1000")):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = ingredient_physical_quantity
    ingredient.weighted_average_unit_cost = Decimal("0.01")
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
    return business, product, order


def test_confirm_with_no_warnings_commits_atomically(session: Session):
    business, product, order = _draft_setup(session)

    confirmed, results = confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert confirmed.status == OrderStatus.CONFIRMED
    assert confirmed.confirmed_at is not None
    history = session.scalars(
        select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id)
    ).all()
    assert [h.to_status for h in history] == [OrderStatus.CONFIRMED]
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("6.000000")
    assert results[0].missing_recipe is False


def test_confirm_with_unacknowledged_shortage_rejects_with_zero_mutation(session: Session):
    business, product, order = _draft_setup(session, ingredient_physical_quantity=Decimal("1"))
    session.commit()  # durable, independent of confirm_order's own internal rollback
    expected_version = order.version

    raised = False
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
        raised = True
        assert exc.code == "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"

    assert raised
    session.expire_all()
    reloaded = session.get(Order, order.id)
    assert reloaded.status == OrderStatus.DRAFT
    assert reloaded.confirmed_at is None
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )
    assert (
        session.scalars(
            select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id)
        ).first()
        is None
    )


def test_ingredient_shortage_warning_identifies_ingredient_with_byte_for_byte_unchanged_fingerprint(
    session: Session,
):
    """Manual Acceptance UX Correction Plan, Finding 2 — the `INGREDIENT_SHORTAGE`
    warning's human-readable `message` now names the Ingredient/unit, but `code`,
    `severity`, `resource`, and `details` (including `fingerprint`) are byte-for-byte
    unchanged from before this change: the fingerprint is built independently of the
    message text in `_collect_warning_fingerprints`, reading only
    `shortage.ingredient_id`/`shortage.shortage_quantity`."""
    business, product, order = _draft_setup(session, ingredient_physical_quantity=Decimal("1"))
    session.commit()  # durable, independent of confirm_order's own internal rollback
    expected_version = order.version
    ingredient = session.scalars(
        select(Ingredient).where(Ingredient.business_id == business.id)
    ).one()

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
    shortage_issue = next(i for i in raised.issues if i["code"] == "INGREDIENT_SHORTAGE")
    assert shortage_issue["severity"] == "WARNING"
    assert shortage_issue["resource"] == "ingredient"
    assert shortage_issue["field"] is None
    assert ingredient.name in shortage_issue["message"]
    assert ingredient.canonical_unit in shortage_issue["message"]
    expected_fingerprint = (
        f"INGREDIENT_SHORTAGE:{shortage_issue['details']['ingredient_id']}:"
        f"{shortage_issue['details']['shortage_quantity']}"
    )
    assert shortage_issue["details"]["fingerprint"] == expected_fingerprint
    assert shortage_issue["details"]["ingredient_id"] == str(ingredient.id)

    # Preview/Confirm parity: acknowledging exactly that unchanged fingerprint
    # allows the identical confirmation to succeed.
    confirmed, _ = confirm_order(
        session,
        business,
        order.id,
        expected_version=expected_version,
        acknowledged_warning_fingerprints={shortage_issue["details"]["fingerprint"]},
        business_today=_TODAY,
    )
    assert confirmed.status == OrderStatus.CONFIRMED


def test_cancel_releases_reservations(session: Session):
    business, product, order = _draft_setup(session)
    confirmed, _ = confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    canceled, _ = cancel_confirmed_order(
        session,
        business,
        order.id,
        expected_version=confirmed.version,
        business_today=_TODAY,
    )

    assert canceled.status == OrderStatus.CANCELED
    assert canceled.canceled_at is not None
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )


# --- Phase 7 Implementation Remediation Plan, Finding 1: `update_confirmed_order` ------


def _edit_payload(
    order: Order, lines: list[OrderLineInput], **overrides
) -> OrderConfirmedEditRequest:
    defaults: dict = dict(
        version=order.version,
        customer_id=order.customer_id,
        fulfillment_date=order.fulfillment_date,
        fulfillment_time=order.fulfillment_time,
        fulfillment_method=order.fulfillment_method,
        fulfillment_details=order.fulfillment_details,
        fulfillment_notes=order.fulfillment_notes,
        internal_notes=order.internal_notes,
        order_adjustment=order.order_adjustment or Decimal("0"),
        adjustment_description=order.adjustment_description,
        manual_tax=order.manual_tax or Decimal("0"),
        lines=lines,
        acknowledged_warning_fingerprints=[],
    )
    defaults.update(overrides)
    return OrderConfirmedEditRequest(**defaults)


def _confirm(session: Session, business, order: Order):
    confirmed, _results = confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    return confirmed


def test_confirmed_quantity_edit_updates_reservations_pin_unchanged(session: Session):
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()

    line = confirmed.lines[0]
    original_version = confirmed.version  # a plain int copy — `edited` is the SAME
    # ORM-identity object as `confirmed` (same session), so comparing against
    # `confirmed.version` after the call would compare the mutated value to itself.
    original_revision_id = session.scalars(
        select(ProductionRequirement.recipe_revision_id).where(
            ProductionRequirement.product_id == product.id
        )
    ).one()

    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("10"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=original_version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert edited.version != original_version
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("10.000000")
    assert requirement.recipe_revision_id == original_revision_id  # pin preserved
    assert results and results[0].requirements


def test_confirmed_price_only_edit_does_not_recalculate(session: Session):
    """Plan v2 §11 table: pricing/notes-only changes are never demand-affecting —
    no recalculation triggered, the existing ProductionRequirement row is left
    completely untouched (same id, same values)."""
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()

    requirement_before = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    requirement_id_before = requirement_before.id

    line = confirmed.lines[0]
    original_version = confirmed.version
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("15.00"),  # price-only change
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=original_version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert results == []  # no recalculation triggered at all
    requirement_after = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement_after.id == requirement_id_before
    assert requirement_after.confirmed_demand_quantity == Decimal("6.000000")
    assert edited.version != original_version  # header/line snapshot still updated


def test_confirmed_edit_with_unacknowledged_warning_rejects_zero_mutation(session: Session):
    business, product, order = _draft_setup(session, ingredient_physical_quantity=Decimal("1000"))
    confirmed = _confirm(session, business, order)
    session.commit()
    expected_version = confirmed.version

    # Shrink the ingredient's stock so a much larger quantity now shortfalls.
    from app.db.models.ingredient import Ingredient

    ingredient_row = session.scalars(
        select(Ingredient).where(Ingredient.business_id == business.id)
    ).one()
    ingredient_row.physical_quantity = Decimal("1")
    session.commit()

    line = confirmed.lines[0]
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("500"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    raised = False
    try:
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            payload,
            expected_version=expected_version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = True
        assert exc.code == "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
    assert raised

    session.expire_all()
    reloaded = session.get(Order, confirmed.id)
    assert reloaded.version == expected_version
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("6.000000")  # unchanged


def test_confirmed_edit_active_run_blocks_protected_line_but_not_sibling_line(session: Session):
    """Final Architecture Lock §C: the active-run precondition is per-line, not
    per-Order — an edit to an unrelated, unprotected line on the same Order must
    remain fully permitted even while a sibling line is production-locked."""
    business = make_business_graph(session)
    protected_product = make_product(session, business, product_type=ProductType.PRODUCED)
    free_product = make_product(session, business, product_type=ProductType.PRODUCED)
    for product in (protected_product, free_product):
        recipe = make_recipe(session, business, product)
        revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
        ingredient = make_ingredient(session, business)
        ingredient.physical_quantity = Decimal("10000")
        make_recipe_revision_ingredient(
            session, business, revision, ingredient, quantity=Decimal("500")
        )
        if product is protected_product:
            protected_revision = revision

    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    protected_line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=protected_product,
        underlying_quantity=Decimal("6"),
    )
    free_line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=free_product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()

    protected_requirement = make_production_requirement(
        session,
        business,
        protected_product,
        protected_revision,
        demand_date=_TODAY,
        confirmed_demand_quantity=Decimal("6"),
        production_demand_quantity=Decimal("6"),
    )
    session.flush()
    make_production_requirement_order(
        session, business, protected_requirement, order, protected_line
    )
    make_production_run(
        session,
        business,
        protected_product,
        protected_revision,
        source_production_requirement_id=protected_requirement.id,
    )
    session.commit()

    # Attempt 1: edit the PROTECTED line's quantity -> rejected.
    payload_protected = _edit_payload(
        order,
        [
            OrderLineInput(
                id=protected_line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=protected_product.id,
                underlying_quantity=Decimal("9"),
                charged_unit_price=Decimal("10.00"),
            ),
            OrderLineInput(
                id=free_line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=free_product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            ),
        ],
    )
    raised = False
    try:
        update_confirmed_order(
            session,
            business,
            order.id,
            payload_protected,
            expected_version=order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = True
        assert exc.code == "ORDER_PRODUCTION_LOCKED"
    assert raised
    session.expire_all()
    order = session.get(Order, order.id)

    # Attempt 2: edit only the FREE line's quantity -> succeeds.
    payload_free = _edit_payload(
        order,
        [
            OrderLineInput(
                id=protected_line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=protected_product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            ),
            OrderLineInput(
                id=free_line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=free_product.id,
                underlying_quantity=Decimal("11"),
                charged_unit_price=Decimal("10.00"),
            ),
        ],
    )
    edited, results = update_confirmed_order(
        session,
        business,
        order.id,
        payload_free,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    assert edited is not None
    free_requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == free_product.id)
    ).one()
    assert free_requirement.confirmed_demand_quantity == Decimal("11.000000")
    # The protected requirement itself is completely untouched.
    still_protected = session.get(ProductionRequirement, protected_requirement.id)
    assert still_protected.confirmed_demand_quantity == Decimal("6.000000")


# --- Phase 7 Final Remediation Correction Plan, Finding 2: line-type-aware demand -----
# detection (`_line_is_demand_affecting_change` must not compare STANDARD_OPTION's
# always-`None` client `underlying_quantity` against the persisted, real column).


def _standard_option_draft_setup(session: Session, *, ingredient_physical_quantity=Decimal("1000")):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    selling_option = make_selling_option(
        session, business, product, quantity_units=6, price=Decimal("12.00")
    )
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = ingredient_physical_quantity
    ingredient.weighted_average_unit_cost = Decimal("0.01")
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
        line_type=OrderLineType.STANDARD_OPTION,
        product=product,
        selling_option=selling_option,
        package_quantity=Decimal("2"),
        underlying_quantity=Decimal("12"),
        charged_unit_price_snapshot=Decimal("12.00"),
        line_subtotal=Decimal("24.00"),
    )
    session.flush()
    return business, product, selling_option, order


def test_confirmed_standard_option_price_only_edit_does_not_recalculate(session: Session):
    """A STANDARD_OPTION price-only edit must never be misclassified as
    demand-affecting merely because the client never submits `underlying_quantity`
    for this line type (it is server-derived from `package_quantity`)."""
    business, product, selling_option, order = _standard_option_draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()

    requirement_before = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    requirement_id_before = requirement_before.id

    line = confirmed.lines[0]
    original_version = confirmed.version
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.STANDARD_OPTION,
                product_id=product.id,
                selling_option_id=selling_option.id,
                package_quantity=Decimal("2"),  # unchanged
                charged_unit_price=Decimal("15.00"),  # price-only change
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=original_version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert results == []  # no recalculation triggered at all
    requirement_after = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement_after.id == requirement_id_before
    assert requirement_after.confirmed_demand_quantity == Decimal("12.000000")
    assert edited.version != original_version  # header/line snapshot still updated


def test_confirmed_protected_standard_option_price_only_edit_is_allowed(session: Session):
    """Proves the second-order bug is fixed too:
    `_reject_if_confirmed_edit_touches_protected_demand` reuses the same helper, so a
    price-only edit to a line whose demand is currently production-locked must be
    allowed — only a genuinely demand-affecting change to a protected line is
    rejected."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    selling_option = make_selling_option(
        session, business, product, quantity_units=6, price=Decimal("12.00")
    )
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
    line = make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.STANDARD_OPTION,
        product=product,
        selling_option=selling_option,
        package_quantity=Decimal("2"),
        underlying_quantity=Decimal("12"),
        charged_unit_price_snapshot=Decimal("12.00"),
        line_subtotal=Decimal("24.00"),
    )
    session.flush()

    requirement = make_production_requirement(
        session,
        business,
        product,
        revision,
        demand_date=_TODAY,
        confirmed_demand_quantity=Decimal("12"),
        production_demand_quantity=Decimal("12"),
    )
    session.flush()
    make_production_requirement_order(session, business, requirement, order, line)
    make_production_run(
        session,
        business,
        product,
        revision,
        source_production_requirement_id=requirement.id,
    )
    session.commit()

    payload = _edit_payload(
        order,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.STANDARD_OPTION,
                product_id=product.id,
                selling_option_id=selling_option.id,
                package_quantity=Decimal("2"),  # unchanged
                charged_unit_price=Decimal("20.00"),  # price-only change
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        order.id,
        payload,
        expected_version=order.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert edited is not None  # not rejected as production-locked
    assert results == []  # price-only, still not demand-affecting -> no recalculation
    still_protected = session.get(ProductionRequirement, requirement.id)
    assert still_protected.confirmed_demand_quantity == Decimal("12.000000")


def test_confirmed_standard_option_package_quantity_change_triggers_recalculation(
    session: Session,
):
    business, product, selling_option, order = _standard_option_draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()

    line = confirmed.lines[0]
    original_version = confirmed.version
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.STANDARD_OPTION,
                product_id=product.id,
                selling_option_id=selling_option.id,
                package_quantity=Decimal("3"),  # 2 -> 3 packages
                charged_unit_price=Decimal("12.00"),
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=original_version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert edited.version != original_version
    assert results and results[0].requirements  # recalculation actually ran
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("18.000000")  # 3 * 6 units/package


def test_confirmed_standard_option_selling_option_change_triggers_recalculation(
    session: Session,
):
    """A Selling-Option change within the same Product is a source change even though
    `product_id` itself is unchanged — a demand-affecting dimension distinct from
    Product identity for STANDARD_OPTION lines."""
    business, product, selling_option, order = _standard_option_draft_setup(session)
    other_option = make_selling_option(
        session, business, product, name="12-pack", quantity_units=12, price=Decimal("20.00")
    )
    session.flush()
    confirmed = _confirm(session, business, order)
    session.commit()

    line = confirmed.lines[0]
    original_version = confirmed.version
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.STANDARD_OPTION,
                product_id=product.id,
                selling_option_id=other_option.id,  # source changed, same Product
                package_quantity=Decimal("2"),
                charged_unit_price=Decimal("20.00"),
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=original_version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert edited.version != original_version
    assert results and results[0].requirements  # recalculation actually ran
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("24.000000")  # 2 * 12 units/package


def test_confirmed_custom_quantity_packaging_and_price_only_edit_does_not_recalculate(
    session: Session,
):
    """CUSTOM_QUANTITY's packaging/price fields are never demand-affecting — only
    `underlying_quantity`/`product_id` are (Plan v2 §11 table)."""
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()

    requirement_before = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    requirement_id_before = requirement_before.id

    line = confirmed.lines[0]
    original_version = confirmed.version
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),  # unchanged
                charged_unit_price=Decimal("25.00"),  # price change
                packaging_cost_per_package=Decimal("1.50"),  # packaging change
            )
        ],
    )

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=original_version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )

    assert results == []  # no operational rebuild
    requirement_after = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement_after.id == requirement_id_before
    assert requirement_after.confirmed_demand_quantity == Decimal("6.000000")
    assert edited.version != original_version


# --- Phase 7 Final Remediation Correction Plan, Finding 1: instrumented lock-graph ------
# ordering proof (Recipe/Ingredient/Surplus locks complete before ANY Order/OrderLine
# write is emitted, by construction — not merely because of the session's own
# `autoflush=False` setting).

_LOCK_TABLES = (
    "recipes",
    "recipe_revisions",
    "recipe_revision_ingredients",
    "ingredients",
    "surplus_inventory",
    "purchased_product_inventory",
)
_MUTATION_PREFIXES = (
    "update orders",
    "insert into order_lines",
    "update order_lines",
    "delete from order_lines",
)


def test_confirmed_edit_acquires_operational_locks_before_any_mutation(session: Session):
    """A demand-affecting confirmed edit must complete every Recipe/Ingredient/Surplus
    lock SELECT before emitting its first Order/OrderLine write. This is the DB-visible
    ordering the Final Architecture Lock §A/§B invariant ("lock -> validate -> mutate")
    actually requires; splitting `order_service._apply_order_edit` into Phase A
    (`_prepare_order_edit`, validate/lock only) and Phase B (`_apply_order_edit_body`,
    reconcile/mutate only) makes it hold by construction rather than by the test
    session's incidental `autoflush=False` configuration."""
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()

    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", _record)
    try:
        line = confirmed.lines[0]
        payload = _edit_payload(
            confirmed,
            [
                OrderLineInput(
                    id=line.id,
                    line_type=OrderLineType.CUSTOM_QUANTITY,
                    product_id=product.id,
                    underlying_quantity=Decimal("10"),  # demand-affecting
                    charged_unit_price=Decimal("10.00"),
                )
            ],
        )
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            payload,
            expected_version=confirmed.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    finally:
        event.remove(connection, "before_cursor_execute", _record)

    lowered = [sql.lower() for sql in statements]
    lock_indices = [
        i
        for i, sql in enumerate(lowered)
        if "for update" in sql and any(table in sql for table in _LOCK_TABLES)
    ]
    mutation_indices = [
        i for i, sql in enumerate(lowered) if sql.strip().startswith(_MUTATION_PREFIXES)
    ]

    assert lock_indices, "expected at least one Recipe/Ingredient lock SELECT"
    assert mutation_indices, "expected at least one Order/OrderLine write"
    assert max(lock_indices) < min(mutation_indices), (
        "a Recipe/Ingredient/Surplus lock SELECT ran AFTER an Order/OrderLine write "
        "was already emitted"
    )


# --- Phase 7 Final Remediation Correction Plan, Finding 5: multi-Product shared- -------
# Ingredient combined candidate-state calculation, real Confirm/confirmed-edit.


def _shared_ingredient_setup(session: Session, *, physical_quantity=Decimal("100")):
    """Two PRODUCED Products, each with a trivial 1-unit-out/1-unit-ingredient-in
    recipe sharing one Ingredient — a demand quantity of N for either Product
    consumes exactly N units of the shared Ingredient, so the combined-shortage
    arithmetic in each test below is transparent (60 + 60 - 100 = 20)."""
    business = make_business_graph(session)
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = physical_quantity
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

    session.flush()
    return business, ingredient, product_a, product_b


def test_confirm_shared_ingredient_shortage_produces_exactly_one_fingerprint(session: Session):
    """Confirming ONE Order with lines for TWO different Products that share an
    Ingredient must read the combined demand across both Products exactly once: a
    real sequential `persist=True` recalculation (Product A processed and flushed,
    then Product B) must never mint two different `INGREDIENT_SHORTAGE`
    fingerprints for the one shared Ingredient — the bug this corrects would let
    each Product's own `ingredient_shortages` entry capture a different
    intermediate quantity (Product A's own entry stale, computed before Product
    B's contribution was flushed)."""
    business, ingredient, product_a, product_b = _shared_ingredient_setup(session)

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_a,
        underlying_quantity=Decimal("60"),
    )
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_b,
        underlying_quantity=Decimal("60"),
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
    assert raised.code == "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
    shortage_issues = [i for i in raised.issues if i["code"] == "INGREDIENT_SHORTAGE"]
    assert len(shortage_issues) == 1, shortage_issues  # never two divergent fingerprints
    assert Decimal(shortage_issues[0]["details"]["shortage_quantity"]) == Decimal("20")
    fingerprint = shortage_issues[0]["details"]["fingerprint"]

    # Zero mutation on the rejected attempt.
    session.expire_all()
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product_a.id)
        ).first()
        is None
    )

    confirmed, results = confirm_order(
        session,
        business,
        order.id,
        expected_version=order.version,
        acknowledged_warning_fingerprints={fingerprint},
        business_today=_TODAY,
    )
    assert confirmed.status == OrderStatus.CONFIRMED
    assert results and len(results) == 2


def test_confirm_shared_ingredient_matches_sequential_two_order_confirm_parity(
    session: Session,
):
    """Parity check: confirming ONE Order with both Products' demand in a single
    request must produce the identical combined shortage as confirming two
    SEPARATE single-Product Orders sequentially (each individual confirm sees the
    prior one's already-committed reservation as genuinely external) — proving the
    combined-request code path isn't silently computing something different from
    what plain sequential confirmation would already correctly produce."""
    # Sequential baseline: two separate Orders, confirmed one after another.
    business, ingredient, product_a, product_b = _shared_ingredient_setup(session)
    order_1 = make_order(session, business, status=OrderStatus.DRAFT)
    order_1.fulfillment_date = _TODAY
    order_1.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order_1,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_a,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    session.commit()
    confirm_order(
        session,
        business,
        order_1.id,
        expected_version=order_1.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    session.commit()

    order_2 = make_order(session, business, status=OrderStatus.DRAFT)
    order_2.fulfillment_date = _TODAY
    order_2.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order_2,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_b,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    session.commit()
    raised = None
    try:
        confirm_order(
            session,
            business,
            order_2.id,
            expected_version=order_2.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc
    assert raised is not None
    sequential_shortage_issue = next(i for i in raised.issues if i["code"] == "INGREDIENT_SHORTAGE")
    assert Decimal(sequential_shortage_issue["details"]["shortage_quantity"]) == Decimal("20")

    # Combined-request case: same physical stock, same two Products, ONE Order.
    business2, ingredient2, product_a2, product_b2 = _shared_ingredient_setup(session)
    combined_order = make_order(session, business2, status=OrderStatus.DRAFT)
    combined_order.fulfillment_date = _TODAY
    combined_order.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business2,
        combined_order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_a2,
        underlying_quantity=Decimal("60"),
    )
    make_order_line(
        session,
        business2,
        combined_order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_b2,
        underlying_quantity=Decimal("60"),
    )
    session.flush()
    session.commit()
    raised2 = None
    try:
        confirm_order(
            session,
            business2,
            combined_order.id,
            expected_version=combined_order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised2 = exc
    assert raised2 is not None
    combined_shortage_issue = next(i for i in raised2.issues if i["code"] == "INGREDIENT_SHORTAGE")
    assert Decimal(combined_shortage_issue["details"]["shortage_quantity"]) == Decimal("20")


def test_confirmed_edit_substitution_shared_ingredient_combined_shortage(session: Session):
    """Confirmed-edit substitution variant (Finding 5's own explicit ask): a
    confirmed edit to Order A's line for shared Product A must combine correctly
    with sibling Order B's own separate demand for shared Product B against their
    common Ingredient — `exclude_order_id` only ever excludes Order A's OWN
    persisted contribution from being double-counted against its own hypothetical
    replacement; it must never accidentally exclude (or fail to combine with)
    Order B's unrelated, still-live contribution to the same shared Ingredient."""
    business, ingredient, product_a, product_b = _shared_ingredient_setup(session)

    order_b = make_order(session, business, status=OrderStatus.DRAFT)
    order_b.fulfillment_date = _TODAY
    order_b.fulfillment_time = time(9, 0)
    make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_b,
        underlying_quantity=Decimal("60"),
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

    order_a = make_order(session, business, status=OrderStatus.DRAFT)
    order_a.fulfillment_date = _TODAY
    order_a.fulfillment_time = time(9, 0)
    line_a = make_order_line(
        session,
        business,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_a,
        underlying_quantity=Decimal("10"),
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

    # Edit Order A's own line from 10 -> 60 — combined with Order B's already-
    # confirmed 60, this now exceeds the shared Ingredient's 100 physical stock
    # (60 + 60 - 100 = 20), a shortage that only exists because of BOTH Orders'
    # combined demand, not either alone.
    payload = _edit_payload(
        confirmed_a,
        [
            OrderLineInput(
                id=line_a.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product_a.id,
                underlying_quantity=Decimal("60"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    raised = None
    try:
        update_confirmed_order(
            session,
            business,
            confirmed_a.id,
            payload,
            expected_version=confirmed_a.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc
    assert raised is not None
    assert raised.code == "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
    shortage_issues = [i for i in raised.issues if i["code"] == "INGREDIENT_SHORTAGE"]
    assert len(shortage_issues) == 1, shortage_issues
    assert Decimal(shortage_issues[0]["details"]["shortage_quantity"]) == Decimal("20")
    fingerprint = shortage_issues[0]["details"]["fingerprint"]

    # Zero mutation on the rejected attempt: Order A's requirement is untouched.
    session.expire_all()
    requirement_a = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product_a.id)
    ).one()
    assert requirement_a.confirmed_demand_quantity == Decimal("10.000000")

    edited, results = update_confirmed_order(
        session,
        business,
        confirmed_a.id,
        payload,
        expected_version=confirmed_a.version,
        acknowledged_warning_fingerprints={fingerprint},
        business_today=_TODAY,
    )
    assert edited is not None
    session.expire_all()
    requirement_a_after = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product_a.id)
    ).one()
    assert requirement_a_after.confirmed_demand_quantity == Decimal("60.000000")


# --- Phase 7 Final Lifecycle Invariant Correction Plan, Finding A: CONFIRMED Order -----
# edits must preserve confirmation structural invariants.


def test_confirmed_edit_rejecting_all_lines_returns_order_not_confirmable(session: Session):
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()
    expected_version = confirmed.version

    payload = _edit_payload(confirmed, [])

    raised = None
    try:
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            payload,
            expected_version=expected_version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"
    assert any(issue["code"] == "ORDER_NO_LINES" for issue in raised.issues)

    session.expire_all()
    reloaded = session.get(Order, confirmed.id)
    assert reloaded.status == OrderStatus.CONFIRMED
    assert reloaded.version == expected_version
    assert len(reloaded.lines) == 1
    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.confirmed_demand_quantity == Decimal("6.000000")


def test_confirmed_edit_clearing_fulfillment_date_returns_order_not_confirmable(session: Session):
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()
    expected_version = confirmed.version
    line = confirmed.lines[0]

    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
        fulfillment_date=None,
    )

    raised = None
    try:
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            payload,
            expected_version=expected_version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"
    assert any(issue["code"] == "ORDER_MISSING_FULFILLMENT_DATE" for issue in raised.issues)

    session.expire_all()
    reloaded = session.get(Order, confirmed.id)
    assert reloaded.fulfillment_date is not None
    assert reloaded.version == expected_version


def test_confirmed_edit_both_violations_returns_both_issues(session: Session):
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()
    expected_version = confirmed.version

    payload = _edit_payload(confirmed, [], fulfillment_date=None)

    raised = None
    try:
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            payload,
            expected_version=expected_version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"
    codes = {issue["code"] for issue in raised.issues}
    assert codes == {"ORDER_NO_LINES", "ORDER_MISSING_FULFILLMENT_DATE"}


def test_confirmed_edit_with_valid_candidate_state_still_succeeds(session: Session):
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()
    line = confirmed.lines[0]

    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("25.00"),
            )
        ],
    )

    edited, _results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=confirmed.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    assert edited.status == OrderStatus.CONFIRMED
    assert edited.lines[0].charged_unit_price_snapshot == Decimal("25.00")


def test_draft_edit_missing_fulfillment_date_remains_allowed(session: Session):
    business, product, order = _draft_setup(session)
    session.commit()
    line = order.lines[0]

    payload = OrderUpdateRequest(
        version=order.version,
        customer_id=order.customer_id,
        fulfillment_date=None,
        fulfillment_time=None,
        lines=[
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
    )

    updated = update_draft_order(session, business, order.id, payload)
    assert updated.status == OrderStatus.DRAFT
    assert updated.fulfillment_date is None


def _confirmed_order_with_protected_line(session: Session):
    """A real CONFIRMED Order with one CUSTOM_QUANTITY line covered by an active
    IN_PRODUCTION run."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
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
    return business, order, protected_line


def test_confirmed_edit_structural_violation_wins_over_production_locked(session: Session):
    """A production-locked Order edited with a structurally-illegal payload
    (`lines=[]`, which necessarily drops the protected line too) must be rejected
    as `ORDER_NOT_CONFIRMABLE`, never masked by `ORDER_PRODUCTION_LOCKED`."""
    business, order, _protected_line = _confirmed_order_with_protected_line(session)

    payload = _edit_payload(order, [])

    raised = None
    try:
        update_confirmed_order(
            session,
            business,
            order.id,
            payload,
            expected_version=order.version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "ORDER_NOT_CONFIRMABLE"


# --- Phase 7 Final Lifecycle Invariant Correction Plan, Finding C: Business-local ------
# DST validity, independent of elapsed-time availability and Product type.


def test_confirm_produced_order_nonexistent_local_time_rejected(session: Session):
    """2026-03-08 is the US spring-forward date for the default America/New_York
    Business timezone -- 02:00-02:59 local time does not exist that day."""
    business, product, order = _draft_setup(session)
    order.fulfillment_date = date(2026, 3, 8)
    order.fulfillment_time = time(2, 30)
    session.commit()
    expected_version = order.version

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
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"
    assert raised.issues[0]["details"]["reason"] == "nonexistent"

    session.expire_all()
    reloaded = session.get(Order, order.id)
    assert reloaded.status == OrderStatus.DRAFT
    assert (
        session.scalars(
            select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
        ).first()
        is None
    )


def test_confirm_produced_order_elapsed_time_none_nonexistent_local_time_still_rejected(
    session: Session,
):
    """Under the OLD code, `elapsed_time_minutes=None` would have made
    `calculate_suggested_start` short-circuit to MISSING_INPUT before ever
    validating the wall-clock -- proves the fix: the invalid time is still
    rejected even when elapsed duration is unavailable."""
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session, business, recipe, yield_quantity=Decimal("12"), elapsed_time_minutes=None
    )
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = date(2026, 3, 8)
    order.fulfillment_time = time(2, 30)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
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
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"


def test_confirm_purchased_only_order_nonexistent_local_time_rejected(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PURCHASED)
    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = date(2026, 3, 8)
    order.fulfillment_time = time(2, 30)
    make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
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
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"


def test_confirm_custom_item_only_order_ambiguous_local_time_rejected(session: Session):
    """2026-11-01 is the US fall-back date -- 01:00-01:59 local time occurs twice."""
    business = make_business_graph(session)
    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = date(2026, 11, 1)
    order.fulfillment_time = time(1, 30)
    make_order_line(session, business, order)  # default line_type is CUSTOM_ITEM
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
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"
    assert raised.issues[0]["details"]["reason"] == "ambiguous"


def test_update_confirmed_order_nonexistent_local_time_rejected(session: Session):
    business, product, order = _draft_setup(session)
    confirmed = _confirm(session, business, order)
    session.commit()
    expected_version = confirmed.version
    line = confirmed.lines[0]

    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("10.00"),
            )
        ],
        fulfillment_date=date(2026, 3, 8),
        fulfillment_time=time(2, 30),
    )

    raised = None
    try:
        update_confirmed_order(
            session,
            business,
            confirmed.id,
            payload,
            expected_version=expected_version,
            acknowledged_warning_fingerprints=set(),
            business_today=_TODAY,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"

    session.expire_all()
    reloaded = session.get(Order, confirmed.id)
    assert reloaded.version == expected_version
    assert reloaded.fulfillment_date == _TODAY


def test_confirm_valid_time_with_missing_elapsed_duration_succeeds_with_null_suggested_start(
    session: Session,
):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(
        session, business, recipe, yield_quantity=Decimal("12"), elapsed_time_minutes=None
    )
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
    assert confirmed.status == OrderStatus.CONFIRMED

    requirement = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == product.id)
    ).one()
    assert requirement.suggested_start_at is None


def _shared_group_setup(session: Session):
    business = make_business_graph(session)
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )
    session.flush()
    return business, product, revision


def test_shared_group_rejects_invalid_contributing_time_not_hidden_by_earliest(session: Session):
    """Two CONFIRMED Orders contributing to the same (product, revision, date)
    group: Order A has a valid, EARLIER time (would win a naive min()); Order B's
    CONFIRMED state is constructed directly (bypassing `confirm_order`'s own
    upfront gate, simulating a contributor that reached this state some other
    way) with an individually-invalid, LATER time. Recalculation must still
    reject -- proving the recalculation layer's own defense, not merely the
    upfront gate, catches this."""
    business, product, revision = _shared_group_setup(session)
    demand_date = date(2026, 3, 8)

    requirement = make_production_requirement(
        session,
        business,
        product,
        revision,
        demand_date=demand_date,
        confirmed_demand_quantity=Decimal("12"),
        production_demand_quantity=Decimal("12"),
    )
    session.flush()

    order_a = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_a.fulfillment_date = demand_date
    order_a.fulfillment_time = time(1, 0)  # valid, earlier
    line_a = make_order_line(
        session,
        business,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    make_production_requirement_order(session, business, requirement, order_a, line_a)

    order_b = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_b.fulfillment_date = demand_date
    order_b.fulfillment_time = time(2, 30)  # nonexistent, later
    line_b = make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    make_production_requirement_order(session, business, requirement, order_b, line_b)
    session.commit()

    raised = None
    try:
        recalculate_product_closure(
            session,
            business,
            product,
            business_today=_TODAY,
            exclude_order_id=None,
            hypothetical_lines=(),
            persist=False,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"
    assert raised.issues[0]["details"]["reason"] == "nonexistent"


def test_shared_group_rejects_invalid_time_not_masked_by_a_missing_time_contributor(
    session: Session,
):
    """Order A has NO fulfillment_time at all (allowed -- confirmation only
    requires a date); Order B has an individually-invalid SUPPLIED time.
    Recalculation must still reject -- proving the missing-time short-circuit
    (`any(t is None ...) -> suggested_start_at=NULL`) no longer masks a
    genuinely invalid supplied time from a different contributor."""
    business, product, revision = _shared_group_setup(session)
    demand_date = date(2026, 3, 8)

    requirement = make_production_requirement(
        session,
        business,
        product,
        revision,
        demand_date=demand_date,
        confirmed_demand_quantity=Decimal("12"),
        production_demand_quantity=Decimal("12"),
    )
    session.flush()

    order_a = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_a.fulfillment_date = demand_date
    order_a.fulfillment_time = None  # missing
    line_a = make_order_line(
        session,
        business,
        order_a,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    make_production_requirement_order(session, business, requirement, order_a, line_a)

    order_b = make_order(session, business, status=OrderStatus.CONFIRMED)
    order_b.fulfillment_date = demand_date
    order_b.fulfillment_time = time(2, 30)  # nonexistent, supplied
    line_b = make_order_line(
        session,
        business,
        order_b,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal("6"),
    )
    session.flush()
    make_production_requirement_order(session, business, requirement, order_b, line_b)
    session.commit()

    raised = None
    try:
        recalculate_product_closure(
            session,
            business,
            product,
            business_today=_TODAY,
            exclude_order_id=None,
            hypothetical_lines=(),
            persist=False,
        )
    except ApiError as exc:
        raised = exc

    assert raised is not None
    assert raised.code == "FULFILLMENT_LOCAL_TIME_INVALID"


def test_confirmed_edit_non_operational_edit_allowed_despite_stale_dst_invalid_stored_time(
    session: Session,
):
    """Confirm an order under a Business timezone with no DST (America/Phoenix) at
    a wall-clock that is perfectly valid there. After confirmation, change
    `business.timezone` to America/New_York (simulating the Business
    correcting/changing its stored timezone after the fact) -- the SAME,
    unchanged, stored wall-clock is now DST-nonexistent under the new zone. A
    price-only edit (fulfillment_date/time unchanged, not demand-affecting) must
    still succeed -- a non-operational edit is not blocked by a stale stored time
    the edit itself never touches."""
    business = make_business_graph(session, timezone="America/Phoenix")
    product = make_product(session, business, product_type=ProductType.PRODUCED)
    recipe = make_recipe(session, business, product)
    revision = make_recipe_revision(session, business, recipe, yield_quantity=Decimal("12"))
    ingredient = make_ingredient(session, business)
    ingredient.physical_quantity = Decimal("10000")
    make_recipe_revision_ingredient(
        session, business, revision, ingredient, quantity=Decimal("500")
    )

    order = make_order(session, business, status=OrderStatus.DRAFT)
    order.fulfillment_date = date(2026, 3, 8)
    order.fulfillment_time = time(2, 30)  # valid in Phoenix (no DST)
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

    # The Business's stored timezone changes -- the same stored wall-clock is now
    # DST-nonexistent under the new zone.
    business.timezone = "America/New_York"
    session.commit()

    line = confirmed.lines[0]
    payload = _edit_payload(
        confirmed,
        [
            OrderLineInput(
                id=line.id,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product_id=product.id,
                underlying_quantity=Decimal("6"),
                charged_unit_price=Decimal("25.00"),  # price-only, not demand-affecting
            )
        ],
    )

    edited, _results = update_confirmed_order(
        session,
        business,
        confirmed.id,
        payload,
        expected_version=confirmed.version,
        acknowledged_warning_fingerprints=set(),
        business_today=_TODAY,
    )
    assert edited.status == OrderStatus.CONFIRMED
    assert edited.lines[0].charged_unit_price_snapshot == Decimal("25.00")
