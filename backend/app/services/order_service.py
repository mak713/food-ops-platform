"""Order/OrderLine business logic (Phase 6 Final Plan + Final Pre-Implementation
Amendment). Payment-specific orchestration lives in `payment_service.py`.

Order is the optimistic-concurrency boundary for the whole mutable Draft aggregate
(Order header + OrderLines) — see `touch_order` below for how a child-only edit still
advances `Order.version`/`updated_at` even when no `orders` column's own value changes.

Phase 6 never persists `DRAFT -> CONFIRMED` (no service function here mutates `status`,
sets `confirmed_at`, or writes a `CONFIRMED` history row) and never writes to any Phase
7+ table (reservations/allocations/production/cost-allocation) — see
`app.domain.order_pricing.check_structural_confirmation_readiness` for the typed,
non-mutating readiness check exposed on `OrderResponse` instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from psycopg.errors import UniqueViolation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.api_errors import ApiError
from app.core.tenant import NotFoundError, check_version, commit_or_raise_stale, stale_version_error
from app.db.enums import OrderLineType, OrderStatus
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.order import Order, OrderLine, OrderStatusHistory, Payment
from app.db.models.product import Product, SellingOption
from app.domain import order_pricing
from app.schemas.order import OrderCreateRequest, OrderLineInput, OrderUpdateRequest

_ORDER_NUMBER_CONSTRAINT = "uq_orders_business_id_order_number"
_MAX_CREATE_ATTEMPTS = 3


# --- Tenant-scoped lookup (mirrors get_product_for_business[_locked] exactly) ----------


def get_order_for_business(db: Session, order_id: uuid.UUID, business: Business) -> Order:
    obj = db.scalar(select(Order).where(Order.id == order_id, Order.business_id == business.id))
    if obj is None:
        raise NotFoundError()
    return obj


def get_order_for_business_locked(db: Session, order_id: uuid.UUID, business: Business) -> Order:
    """Tenant-scoped `SELECT ... FOR UPDATE` on the Order row — the shared serialization
    point for every Order-aggregate mutation (`update_draft_order`, `delete_draft_order`)
    and for Payment addition (`payment_service.add_payment`), so a concurrent edit and a
    concurrent Payment always queue against the same lock rather than racing (Final Plan
    §G). Scoped by `(id, business_id)` in the query itself, matching
    `get_product_for_business_locked` exactly."""
    obj = db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.business_id == business.id)
        .with_for_update()
    )
    if obj is None:
        raise NotFoundError()
    return obj


def touch_order(order: Order) -> None:
    """Forces a real `UPDATE orders ...` against this row — and therefore always advances
    `version` via `version_id_col` — even when a Draft edit only changed child OrderLine
    rows and every other `orders` column's own value happens to end up numerically
    unchanged (Final Pre-Implementation Amendment §1's blocker fix).

    Assigns a genuine new value (a SQL expression, `func.now()`) rather than merely
    flagging the already-loaded Python value as "modified" — an earlier draft of this
    helper used `sqlalchemy.orm.attributes.flag_modified(order, "updated_at")`, which does
    force `Session.dirty` membership and does cause `version_id_col` to bump, but does
    NOT assign a new value: at flush, SQLAlchemy then writes the *existing* in-memory
    `updated_at` back unchanged, because `onupdate=func.now()` only fires for a column
    with *no* history entry at all for that flush, and `flag_modified` establishes one
    (with no new value) regardless. Assigning `func.now()` sidesteps this: the database
    computes a real, new timestamp, `Order.updated_at` genuinely advances, and
    `version_id_col` bumps for the same reason as any other real column edit — the
    instance is dirty, for any reason, so version participates in the resulting UPDATE."""
    order.updated_at = func.now()


def _generate_order_number(order_id: uuid.UUID) -> str:
    """`ORD-` + the first 12 uppercase hex characters of the Order's own already-globally-
    unique `id` (Final Plan §H; Amendment §1/§16) — no `MAX()+1`, no separate sequence,
    no schema change, no reuse-after-deletion problem: a new Order always gets a fresh
    `id`, hence a fresh token, regardless of how many Drafts were previously deleted.
    `UNIQUE(business_id, order_number)` remains the DB backstop; see `create_draft_order`'s
    bounded, whole-transaction retry for the (astronomically unlikely) collision case."""
    return f"ORD-{order_id.hex[:12].upper()}"


def _is_order_number_conflict(exc: IntegrityError) -> bool:
    return (
        isinstance(exc.orig, UniqueViolation)
        and exc.orig.diag.constraint_name == _ORDER_NUMBER_CONSTRAINT
    )


def payment_overage_error(
    *, existing_total: Decimal, final_total: Decimal, proposed: Decimal
) -> ApiError:
    """The overpayment warning/acknowledgment envelope (Final Plan §G), shared by
    `payment_service.add_payment` (a proposed new Payment would overpay) and
    `update_draft_order` (a lowered total would retroactively overpay already-recorded
    Payments) — mirrors `customer_service`'s `POSSIBLE_DUPLICATE_CUSTOMER` warning shape
    exactly (ADR-102's pattern, reused rather than reinvented)."""
    is_payment_side = proposed > 0
    return ApiError(
        422,
        "PAYMENT_OVERAGE_WARNING",
        "This would result in the order being overpaid.",
        issues=[
            {
                "severity": "WARNING",
                "code": "PAYMENT_WOULD_OVERPAY",
                "message": (
                    "Recording this would exceed the order's total. Confirm to record it "
                    "as an overpayment."
                    if is_payment_side
                    else (
                        "This total is lower than the payments already recorded against "
                        "this order. Confirm to accept the resulting overpayment."
                    )
                ),
                "field": "amount" if is_payment_side else "final_total",
                "resource": "payment" if is_payment_side else "order",
                "details": {
                    "existing_payments_total": str(existing_total),
                    "final_total": str(final_total),
                    "proposed_amount": str(proposed),
                },
            }
        ],
    )


# --- Reference validation + cross-model-type deterministic lock ordering --------------


def _ref_issue(field: str, code: str, resource: str, message: str) -> dict:
    return {
        "severity": "ERROR",
        "code": code,
        "message": message,
        "field": field,
        "resource": resource,
        "details": {},
    }


def _validate_and_lock_order_references(
    db: Session,
    business: Business,
    *,
    customer_id: uuid.UUID | None,
    existing_customer_id: uuid.UUID | None,
    lines: list[OrderLineInput],
    existing_lines_by_id: dict[uuid.UUID, OrderLine],
    additional_product_ids: frozenset[uuid.UUID] = frozenset(),
    lock: bool = True,
) -> tuple[Customer | None, dict[uuid.UUID, Product], dict[uuid.UUID, SellingOption]]:
    """Validates+locks every NEWLY INTRODUCED reference — the Order-header `customer_id`
    (only if it differs from `existing_customer_id`) and each line's `product_id`/
    `selling_option_id` (only if new, or changed from the existing line's stored value) —
    in the fixed **Customer -> Product -> Selling Option** group order, IDs sorted within
    each group (Final Pre-Implementation Amendment §12; ADR-106/107/108). Carried-forward
    references on an unchanged header/retained line are never re-validated or locked.
    Collects every invalid/inactive/wrong-product reference into one 422 (never stops at
    the first) — the non-disclosing shape already established for body-embedded
    references (ADR-107): missing, foreign-tenant, and (for a Selling Option) belonging to
    a different Product than declared are all indistinguishable "not found" outcomes.

    `additional_product_ids` (Phase 7 Implementation Remediation Plan, Finding 1; Final
    Architecture Lock §B) — every Product whose operational closure a Phase 7 confirmed
    edit will recalculate, a SUPERSET of whatever this function's own reference-diff
    logic would lock on its own (a quantity-only edit to an unchanged line reference
    still needs that Product locked before recalculating, even though the reference
    itself never changed). Folded into the SAME sorted Product lock query below — never
    a second, later lock pass. An id present only because of this parameter is locked
    for serialization ONLY; it is never subjected to the active-state
    (PRODUCT_NOT_FOUND/PRODUCT_NOT_ACTIVE) rejection below, which applies only to the
    genuinely newly-selected reference subset (ADR-108 carried-forward semantics).

    `lock` (Phase 7 Final Semantic & Precision Correction Plan, Finding 6) — real
    Create/Edit workflows never pass this (default `True`, unchanged locking
    behavior). Preview's own read-only, zero-write call passes `lock=False` so its
    reference resolution acquires no PostgreSQL row locks at all, since it always
    rolls back immediately after — every other validation rule (tenant scoping,
    active/new-reference checks, SellingOption/Product consistency) is identical
    code either way, reused rather than duplicated."""
    issues: list[dict] = []

    # --- Group 1: Customer (header-level, at most one row) ---
    customer: Customer | None = None
    customer_changed = customer_id != existing_customer_id
    if customer_changed and customer_id is not None:
        customer_query = select(Customer).where(
            Customer.id == customer_id, Customer.business_id == business.id
        )
        if lock:
            customer_query = customer_query.with_for_update()
        customer = db.scalar(customer_query)
        if customer is None:
            issues.append(
                _ref_issue(
                    "customer_id",
                    "CUSTOMER_NOT_FOUND",
                    "customer",
                    "The referenced customer could not be found.",
                )
            )
        elif not customer.is_active:
            issues.append(
                _ref_issue(
                    "customer_id",
                    "CUSTOMER_NOT_ACTIVE",
                    "customer",
                    "This customer is archived and cannot be newly selected.",
                )
            )

    # --- Determine which line-level references are newly introduced ---
    # For STANDARD_OPTION, the Product+Selling Option pair is a single source: if EITHER
    # member changes, the whole pair is treated as newly selected — both are locked and
    # both must be active, and the Selling Option must belong to the (possibly-unchanged)
    # Product (Checkpoint-3 correction: Case A — an unrelated Selling-Option-only change
    # under a Product that has since become archived must not slip through merely because
    # the Product's own id didn't change; Case B — a Product change while resubmitting the
    # same, now-mismatched Selling Option id must produce a clean 422, not a missing-key
    # crash). CUSTOM_QUANTITY has no Selling Option at all, so it keeps the simpler
    # product-only rule.
    product_ids: set[uuid.UUID] = set()
    selling_option_ids: set[uuid.UUID] = set()
    for line in lines:
        existing = existing_lines_by_id.get(line.id) if line.id is not None else None
        product_changed = existing is None or existing.product_id != line.product_id
        option_changed = existing is None or existing.selling_option_id != line.selling_option_id
        if line.line_type is OrderLineType.STANDARD_OPTION:
            source_changed = product_changed or option_changed
            if source_changed:
                if line.product_id is not None:
                    product_ids.add(line.product_id)
                if line.selling_option_id is not None:
                    selling_option_ids.add(line.selling_option_id)
        elif line.product_id is not None and product_changed:
            product_ids.add(line.product_id)

    # --- Group 2: Product (Final Architecture Lock §B: reference-newly-changed ids
    # UNION Phase 7's operationally-affected superset, locked in ONE query — never a
    # reference-lock pass followed later by a separate operational-lock pass) ---
    products: dict[uuid.UUID, Product] = {}
    all_product_ids = product_ids | additional_product_ids
    if all_product_ids:
        product_query = (
            select(Product)
            .where(Product.id.in_(sorted(all_product_ids)), Product.business_id == business.id)
            .order_by(Product.id)
        )
        if lock:
            product_query = product_query.with_for_update()
        rows = db.scalars(product_query).all()
        products = {row.id: row for row in rows}

    # --- Group 3: Selling Option (locked by its own id — a single Order may contain
    # STANDARD_OPTION lines under several different Products, so no shared product_id is
    # assumed in the lock query itself; the declared-vs-actual Product match is checked
    # below, in Python, against the already-locked row) ---
    selling_options: dict[uuid.UUID, SellingOption] = {}
    if selling_option_ids:
        option_query = (
            select(SellingOption)
            .where(
                SellingOption.id.in_(sorted(selling_option_ids)),
                SellingOption.business_id == business.id,
            )
            .order_by(SellingOption.id)
        )
        if lock:
            option_query = option_query.with_for_update()
        rows = db.scalars(option_query).all()
        selling_options = {row.id: row for row in rows}

    # --- Per-line validation against the now-locked rows ---
    for index, line in enumerate(lines):
        existing = existing_lines_by_id.get(line.id) if line.id is not None else None
        product_changed = existing is None or existing.product_id != line.product_id
        option_changed = existing is None or existing.selling_option_id != line.selling_option_id

        if line.line_type is OrderLineType.STANDARD_OPTION:
            source_changed = product_changed or option_changed
            if source_changed:
                product = products.get(line.product_id)
                if product is None:
                    issues.append(
                        _ref_issue(
                            f"lines[{index}].product_id",
                            "PRODUCT_NOT_FOUND",
                            "order_line",
                            "The referenced product could not be found.",
                        )
                    )
                elif not product.is_active:
                    issues.append(
                        _ref_issue(
                            f"lines[{index}].product_id",
                            "PRODUCT_NOT_ACTIVE",
                            "order_line",
                            "This product is archived and cannot be newly selected.",
                        )
                    )

                option = selling_options.get(line.selling_option_id)
                if option is None or option.product_id != line.product_id:
                    issues.append(
                        _ref_issue(
                            f"lines[{index}].selling_option_id",
                            "SELLING_OPTION_NOT_FOUND",
                            "order_line",
                            "The referenced selling option could not be found for this product.",
                        )
                    )
                elif not option.is_active:
                    issues.append(
                        _ref_issue(
                            f"lines[{index}].selling_option_id",
                            "SELLING_OPTION_NOT_ACTIVE",
                            "order_line",
                            "This selling option is archived and cannot be newly selected.",
                        )
                    )
        elif line.product_id is not None and product_changed:
            product = products.get(line.product_id)
            if product is None:
                issues.append(
                    _ref_issue(
                        f"lines[{index}].product_id",
                        "PRODUCT_NOT_FOUND",
                        "order_line",
                        "The referenced product could not be found.",
                    )
                )
            elif not product.is_active:
                issues.append(
                    _ref_issue(
                        f"lines[{index}].product_id",
                        "PRODUCT_NOT_ACTIVE",
                        "order_line",
                        "This product is archived and cannot be newly selected.",
                    )
                )

    if issues:
        raise ApiError(
            422,
            "ORDER_INVALID_REFERENCES",
            "One or more referenced records are invalid.",
            issues=issues,
        )
    return customer, products, selling_options


# --- Line-snapshot reconciliation (the four independent per-dimension checks) ----------


def _snapshot_to_columns(snapshot: order_pricing.LineSnapshot) -> dict:
    """Quantizes every derived field exactly once and validates representability before
    it is ever assigned to a mapped column (Final Plan §F)."""
    package_quantity = order_pricing.quantize_quantity(snapshot.package_quantity)
    underlying_quantity = order_pricing.quantize_quantity(snapshot.underlying_quantity)
    charged_unit_price_snapshot = order_pricing.quantize_money(snapshot.charged_unit_price_snapshot)
    line_subtotal = order_pricing.quantize_money(snapshot.line_subtotal)
    packaging_cost_per_package_snapshot = order_pricing.quantize_money(
        snapshot.packaging_cost_per_package_snapshot
    )
    packaging_cost_total_snapshot = order_pricing.quantize_money(
        snapshot.packaging_cost_total_snapshot
    )

    for name, value in (
        ("package_quantity", package_quantity),
        ("underlying_quantity", underlying_quantity),
    ):
        if not order_pricing.is_representable_in_numeric_18_6(value):
            raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", f"{name} is too large to store.")
        if value <= 0:
            raise ApiError(422, "ORDER_LINE_VALUE_INVALID", f"{name} must be greater than zero.")
    for name, value in (
        ("charged_unit_price_snapshot", charged_unit_price_snapshot),
        ("line_subtotal", line_subtotal),
        ("packaging_cost_per_package_snapshot", packaging_cost_per_package_snapshot),
        ("packaging_cost_total_snapshot", packaging_cost_total_snapshot),
    ):
        if not order_pricing.is_representable_in_numeric_14_2(value):
            raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", f"{name} is too large to store.")
        if value < 0:
            raise ApiError(422, "ORDER_LINE_VALUE_INVALID", f"{name} must not be negative.")

    return {
        "display_name_snapshot": snapshot.display_name_snapshot,
        "package_quantity": package_quantity,
        "underlying_quantity": underlying_quantity,
        "charged_unit_price_snapshot": charged_unit_price_snapshot,
        "line_subtotal": line_subtotal,
        "packaging_cost_per_package_snapshot": packaging_cost_per_package_snapshot,
        "packaging_cost_total_snapshot": packaging_cost_total_snapshot,
    }


def _apply_standard_option_line(
    line: OrderLineInput,
    existing: OrderLine | None,
    products: dict[uuid.UUID, Product],
    selling_options: dict[uuid.UUID, SellingOption],
) -> dict | None:
    """STANDARD_OPTION's three relevant edit classes (Final Plan §D.2 / Amendment §3):
    source change, price-only, quantity-only. Returns `None` for a fully unrelated edit —
    Class A — leaving the row completely untouched."""
    source_changed = (
        existing is None
        or existing.product_id != line.product_id
        or existing.selling_option_id != line.selling_option_id
    )
    price_override_supplied = line.charged_unit_price is not None

    if source_changed:
        product = products[line.product_id]
        option = selling_options[line.selling_option_id]
        charged_price = line.charged_unit_price if price_override_supplied else option.price
        override_reason = line.price_override_reason if price_override_supplied else None
        snapshot = order_pricing.calculate_standard_option_line(
            display_name=f"{product.name} — {option.name}",
            package_quantity=line.package_quantity,
            underlying_units_per_package=option.quantity_units,
            charged_unit_price=charged_price,
            packaging_cost_per_package=option.packaging_cost,
        )
        columns = _snapshot_to_columns(snapshot)
        columns.update(
            product_id=product.id,
            selling_option_id=option.id,
            price_override_reason=override_reason,
            notes=line.notes,
        )
        return columns

    assert existing is not None  # a brand-new line always has source_changed=True

    price_changed = (
        price_override_supplied and line.charged_unit_price != existing.charged_unit_price_snapshot
    )
    quantity_changed = line.package_quantity != existing.package_quantity
    notes_changed = (line.notes or None) != existing.notes

    if not price_changed and not quantity_changed:
        if notes_changed:
            return {"notes": line.notes}
        return None  # Class A: unrelated edit, row left completely untouched

    charged_price = existing.charged_unit_price_snapshot
    override_reason = existing.price_override_reason
    if price_changed:
        charged_price = line.charged_unit_price
        override_reason = line.price_override_reason

    if quantity_changed:
        snapshot = order_pricing.rescale_standard_option_line(
            display_name=existing.display_name_snapshot,
            new_package_quantity=line.package_quantity,
            stored_package_quantity=existing.package_quantity,
            stored_underlying_quantity=existing.underlying_quantity,
            stored_charged_unit_price_snapshot=charged_price,
            stored_packaging_cost_per_package_snapshot=existing.packaging_cost_per_package_snapshot,
        )
        columns = _snapshot_to_columns(snapshot)
        columns.update(price_override_reason=override_reason, notes=line.notes)
        return columns

    # price_changed only — packaging/quantity/display-name untouched.
    line_subtotal = order_pricing.calculate_line_subtotal(existing.package_quantity, charged_price)
    charged_price_q = order_pricing.quantize_money(charged_price)
    line_subtotal_q = order_pricing.quantize_money(line_subtotal)
    if not order_pricing.is_representable_in_numeric_14_2(charged_price_q):
        raise ApiError(
            422, "ORDER_LINE_VALUE_OVERFLOW", "charged_unit_price_snapshot is too large to store."
        )
    if not order_pricing.is_representable_in_numeric_14_2(line_subtotal_q):
        raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", "line_subtotal is too large to store.")
    return {
        "charged_unit_price_snapshot": charged_price_q,
        "price_override_reason": override_reason,
        "line_subtotal": line_subtotal_q,
        "notes": line.notes,
    }


def _apply_custom_quantity_line(
    line: OrderLineInput,
    existing: OrderLine | None,
    products: dict[uuid.UUID, Product],
) -> dict | None:
    """CUSTOM_QUANTITY (Final Plan §D.2/§E; Amendment §3/§4): `package_quantity` is
    always `1`. There is no Product catalog price to snapshot for this line type — the
    seller-submitted `underlying_quantity`/`charged_unit_price` are always taken literally
    once the line is touched at all. A Product source change updates only display-name
    identity and resets the packaging default to the new Product's own default (unless a
    packaging override accompanies the same request)."""
    source_changed = existing is None or existing.product_id != line.product_id
    packaging_override_supplied = line.packaging_cost_per_package is not None

    if existing is not None and not source_changed:
        quantity_or_price_changed = (
            line.underlying_quantity != existing.underlying_quantity
            or line.charged_unit_price != existing.charged_unit_price_snapshot
            or (line.price_override_reason or None) != existing.price_override_reason
        )
        packaging_changed = (
            packaging_override_supplied
            and line.packaging_cost_per_package != existing.packaging_cost_per_package_snapshot
        )
        notes_changed = (line.notes or None) != existing.notes

        if not quantity_or_price_changed and not packaging_changed:
            if notes_changed:
                return {"notes": line.notes}
            return None  # Class A: unrelated edit

        display_name = existing.display_name_snapshot
        packaging_cost = (
            line.packaging_cost_per_package
            if packaging_override_supplied
            else existing.packaging_cost_per_package_snapshot
        )
    else:
        product = products[line.product_id]
        display_name = product.name
        packaging_cost = (
            line.packaging_cost_per_package
            if packaging_override_supplied
            else product.default_packaging_cost
        )

    snapshot = order_pricing.calculate_custom_quantity_line(
        display_name=display_name,
        underlying_quantity=line.underlying_quantity,
        agreed_line_price=line.charged_unit_price,
        packaging_cost_per_package=packaging_cost,
    )
    columns = _snapshot_to_columns(snapshot)
    columns.update(
        product_id=line.product_id,
        price_override_reason=line.price_override_reason,
        notes=line.notes,
    )
    return columns


def _apply_custom_item_line(line: OrderLineInput, existing: OrderLine | None) -> dict | None:
    """CUSTOM_ITEM (Final Plan §D.2/§E; Amendment §4/§7): no source-identity dimension
    exists at all. `manual_fulfillment_required` defaults to `True` for a brand-new line
    when omitted (a Custom Item has no Product/Recipe automation to prove fulfillment —
    the safe default requires manual satisfaction); an omitted value on a retained line
    preserves whatever is already stored, rather than being silently reset."""
    if existing is not None:
        manual_required = (
            line.manual_fulfillment_required
            if line.manual_fulfillment_required is not None
            else existing.manual_fulfillment_required
        )
        unchanged = (
            line.underlying_quantity == existing.underlying_quantity
            and line.charged_unit_price == existing.charged_unit_price_snapshot
            and (line.display_name or None) == existing.display_name_snapshot
            and line.custom_direct_cost_estimate == existing.custom_direct_cost_estimate
            and line.custom_active_time_minutes == existing.custom_active_time_minutes
            and manual_required == existing.manual_fulfillment_required
            and (line.notes or None) == existing.notes
        )
        if unchanged:
            return None  # Class A: unrelated edit
    else:
        manual_required = (
            line.manual_fulfillment_required
            if line.manual_fulfillment_required is not None
            else True
        )

    snapshot = order_pricing.calculate_custom_item_line(
        display_name=line.display_name,
        quantity=line.underlying_quantity,
        unit_price=line.charged_unit_price,
    )
    columns = _snapshot_to_columns(snapshot)

    custom_direct_cost_estimate = None
    if line.custom_direct_cost_estimate is not None:
        custom_direct_cost_estimate = order_pricing.quantize_money(line.custom_direct_cost_estimate)
        if not order_pricing.is_representable_in_numeric_14_2(custom_direct_cost_estimate):
            raise ApiError(
                422,
                "ORDER_LINE_VALUE_OVERFLOW",
                "custom_direct_cost_estimate is too large to store.",
            )

    columns.update(
        custom_direct_cost_estimate=custom_direct_cost_estimate,
        custom_active_time_minutes=line.custom_active_time_minutes,
        manual_fulfillment_required=manual_required,
        manual_fulfillment_satisfied=False,
        notes=line.notes,
    )
    return columns


def _reconcile_line(
    line: OrderLineInput,
    existing: OrderLine | None,
    products: dict[uuid.UUID, Product],
    selling_options: dict[uuid.UUID, SellingOption],
) -> dict | None:
    """Dispatches to the per-line-type reconciliation function. Neither
    `_apply_standard_option_line` nor `_apply_custom_quantity_line` ever includes
    `manual_fulfillment_required`/`manual_fulfillment_satisfied` in their returned
    columns (Final Plan §E: forced `False`, not editable, for these two line types) — at
    creation this leaves the columns' own `server_default="false"` in effect; on an
    update to a retained line, omitting the keys simply never touches them, so they stay
    at whatever they already are (always `False`, since no path ever sets them
    otherwise)."""
    if line.line_type.value == "STANDARD_OPTION":
        return _apply_standard_option_line(line, existing, products, selling_options)
    if line.line_type.value == "CUSTOM_QUANTITY":
        return _apply_custom_quantity_line(line, existing, products)
    return _apply_custom_item_line(line, existing)


# --- Order aggregate mutation -----------------------------------------------------------


def list_orders_for_business(
    db: Session,
    business: Business,
    *,
    q: str | None = None,
    status: OrderStatus | None = None,
    customer_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Order], int]:
    conditions = [Order.business_id == business.id]
    if q:
        conditions.append(Order.order_number.ilike(f"%{q}%"))
    if status is not None:
        conditions.append(Order.status == status)
    if customer_id is not None:
        conditions.append(Order.customer_id == customer_id)

    total = db.scalar(select(func.count()).select_from(Order).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Order)
            .where(*conditions)
            .order_by(Order.created_at.desc(), Order.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def create_draft_order(db: Session, business: Business, payload: OrderCreateRequest) -> Order:
    """Atomic Order + Lines + optional initial Payment + one `NULL->DRAFT` history row,
    one commit (Final Plan §I/§H). Bounded retry (Amendment §3): on the astronomically
    unlikely `order_number` unique conflict, the ENTIRE attempt restarts from scratch —
    fresh reference locks, fresh validation, a fresh Order id/order_number — since
    `db.rollback()` releases every lock the previous attempt held; no ORM object or lock
    from a rolled-back attempt is ever reused."""
    last_exc: IntegrityError | None = None
    for _attempt in range(_MAX_CREATE_ATTEMPTS):
        try:
            customer, products, selling_options = _validate_and_lock_order_references(
                db,
                business,
                customer_id=payload.customer_id,
                existing_customer_id=None,
                lines=payload.lines,
                existing_lines_by_id={},
            )
        except ApiError:
            db.rollback()
            raise

        order_id = uuid.uuid4()
        order = Order(
            id=order_id,
            business_id=business.id,
            order_number=_generate_order_number(order_id),
            status=OrderStatus.DRAFT,
            customer_id=customer.id if customer is not None else payload.customer_id,
            fulfillment_date=payload.fulfillment_date,
            fulfillment_time=payload.fulfillment_time,
            fulfillment_method=payload.fulfillment_method,
            fulfillment_details=payload.fulfillment_details,
            fulfillment_notes=payload.fulfillment_notes,
            internal_notes=payload.internal_notes,
            order_adjustment=order_pricing.quantize_money(payload.order_adjustment),
            adjustment_description=payload.adjustment_description,
            manual_tax=order_pricing.quantize_money(payload.manual_tax),
        )
        db.add(order)

        # Every domain/calculator ApiError raised past this point (line reconciliation,
        # snapshot representability, totals) happens after the reference locks above were
        # already acquired — roll back before propagating so no lock is held past an
        # expected rejection (Checkpoint-3 correction: locked rejections must be
        # universally rolled back, not only the explicit ones already covered elsewhere).
        try:
            line_subtotals: list[Decimal] = []
            for line in payload.lines:
                columns = _reconcile_line(line, None, products, selling_options)
                assert columns is not None  # a brand-new line is never "unrelated"
                order_line = OrderLine(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    order_id=order.id,
                    line_type=line.line_type,
                    **columns,
                )
                db.add(order_line)
                line_subtotals.append(order_line.line_subtotal)

            totals = order_pricing.calculate_order_totals(
                line_subtotals,
                order_adjustment=order.order_adjustment,
                manual_tax=order.manual_tax,
            )
            order.subtotal = order_pricing.quantize_money(totals.subtotal)
            order.final_total = order_pricing.quantize_money(totals.final_total)
        except ApiError:
            db.rollback()
            raise

        # Subtotal and final_total are each independently validated for NUMERIC(14,2)
        # representability — line subtotals may each individually fit while their sum
        # does not, even when a later negative adjustment brings final_total back into
        # range (Checkpoint-3 correction 10).
        if not order_pricing.is_representable_in_numeric_14_2(order.subtotal):
            db.rollback()
            raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", "subtotal is too large to store.")
        if order.final_total < 0:
            db.rollback()
            raise ApiError(422, "ORDER_FINAL_TOTAL_NEGATIVE", "The order total cannot be negative.")
        if not order_pricing.is_representable_in_numeric_14_2(order.final_total):
            db.rollback()
            raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", "final_total is too large to store.")

        db.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                business_id=business.id,
                order_id=order.id,
                from_status=None,
                to_status=OrderStatus.DRAFT,
                changed_at=datetime.now(UTC),
            )
        )

        if payload.payment is not None:
            overpays = payload.payment.amount > order.final_total
            if overpays and not payload.payment.confirm_overpayment:
                db.rollback()
                raise payment_overage_error(
                    existing_total=Decimal("0"),
                    final_total=order.final_total,
                    proposed=payload.payment.amount,
                )
            db.add(
                Payment(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    order_id=order.id,
                    amount=payload.payment.amount,
                    payment_method=payload.payment.payment_method,
                    payment_date=payload.payment.payment_date,
                    notes=payload.payment.notes,
                )
            )

        try:
            db.commit()
            return order
        except IntegrityError as exc:
            db.rollback()
            if not _is_order_number_conflict(exc):
                raise
            last_exc = exc
            continue

    raise last_exc or RuntimeError("create_draft_order: exhausted retry attempts")


def _prepare_order_edit(
    db: Session,
    business: Business,
    order: Order,
    payload: OrderUpdateRequest,
    *,
    additional_product_ids: frozenset[uuid.UUID] = frozenset(),
) -> tuple[
    dict[uuid.UUID, Product],
    dict[uuid.UUID, SellingOption],
    dict[uuid.UUID, OrderLine],
    set[uuid.UUID],
    Decimal,
]:
    """Phase A of the shared, non-committing full-reconciliation core (Phase 7 Final
    Remediation Correction Plan, Finding 1) — validate/lock ONLY. No Order/OrderLine
    ORM mutation happens anywhere in this function; a caller that needs to interleave
    further lock acquisition (Recipe/Ingredient/Surplus, for a Phase 7 confirmed edit)
    between reference-locking and mutation can now do so by construction, rather than
    relying on the session's `autoflush=False` setting to accidentally prevent the old
    combined function's mutation from reaching PostgreSQL too early.

    The caller has already locked the Order row and checked its version; this
    function does NOT commit and does NOT check/enforce `order.status`. Every
    locked-then-rejected path below rolls back before raising (Amendment §2) so no
    lock is held past an expected rejection.

    Returns `(products, selling_options, existing_lines_by_id, submitted_ids,
    payments_total)` — everything Phase B (`_apply_order_edit_body`) needs to
    reconcile and mutate, plus the full locked-Product dict (newly-referenced ids
    UNION `additional_product_ids`) so a Phase 7 caller can continue the composed
    lock chain and recalculate every operationally-affected Product's closure
    without a second Product lock pass (Final Architecture Lock §B)."""
    # Fresh Payment sum, queried while holding the Order row lock, BEFORE any Order/
    # OrderLine mutation begins (Final Plan §G; Amendment §7) — deliberately sequenced
    # here rather than immediately before its point of use: once header/line mutations
    # start in Phase B, this same query would trigger SQLAlchemy's autoflush (flushing
    # the not-yet-fully-computed pending changes early, as a separate UPDATE, and
    # bumping `Order.version` prematurely — then `touch_order`'s later, deliberate
    # UPDATE would bump it a second time for the same logical edit). Querying it now,
    # while nothing is yet dirty, avoids that entirely while still satisfying "fresh,
    # post-lock, direct aggregate query, never a pre-loaded relationship."
    payments_total = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.order_id == order.id)
    )

    submitted_ids: set[uuid.UUID] = set()
    for line in payload.lines:
        if line.id is not None:
            if line.id in submitted_ids:
                db.rollback()
                raise ApiError(
                    422,
                    "ORDER_DUPLICATE_LINE_ID",
                    "A line id was submitted more than once.",
                    issues=[
                        {
                            "severity": "ERROR",
                            "code": "DUPLICATE_ORDER_LINE_ID",
                            "message": "Each OrderLine id may appear at most once per request.",
                            "field": "lines",
                            "resource": "order_line",
                            "details": {"line_id": str(line.id)},
                        }
                    ],
                )
            submitted_ids.add(line.id)

    existing_lines_by_id: dict[uuid.UUID, OrderLine] = {line.id: line for line in order.lines}
    unknown_ids = submitted_ids - set(existing_lines_by_id.keys())
    if not unknown_ids:
        for line in payload.lines:
            existing = existing_lines_by_id.get(line.id) if line.id is not None else None
            if existing is not None and existing.line_type != line.line_type:
                db.rollback()
                raise ApiError(
                    422,
                    "ORDER_LINE_TYPE_IMMUTABLE",
                    "An existing order line's type cannot be changed in place.",
                    issues=[
                        {
                            "severity": "ERROR",
                            "code": "ORDER_LINE_TYPE_IMMUTABLE",
                            "message": (
                                "This line's type cannot be changed; delete it and add a "
                                "new line of the desired type instead."
                            ),
                            "field": "lines",
                            "resource": "order_line",
                            "details": {"line_id": str(line.id)},
                        }
                    ],
                )
    if unknown_ids:
        db.rollback()
        raise ApiError(
            422,
            "ORDER_UNKNOWN_LINE_ID",
            "One or more submitted line ids do not belong to this order.",
            issues=[
                {
                    "severity": "ERROR",
                    "code": "UNKNOWN_ORDER_LINE_ID",
                    "message": "This line id does not belong to this order.",
                    "field": "lines",
                    "resource": "order_line",
                    "details": {"line_id": str(line_id)},
                }
                for line_id in sorted(unknown_ids)
            ],
        )

    try:
        _customer, products, selling_options = _validate_and_lock_order_references(
            db,
            business,
            customer_id=payload.customer_id,
            existing_customer_id=order.customer_id,
            lines=payload.lines,
            existing_lines_by_id=existing_lines_by_id,
            additional_product_ids=additional_product_ids,
        )
    except ApiError:
        db.rollback()
        raise

    return products, selling_options, existing_lines_by_id, submitted_ids, payments_total


def _apply_order_edit_body(
    db: Session,
    business: Business,
    order: Order,
    payload: OrderUpdateRequest,
    *,
    products: dict[uuid.UUID, Product],
    selling_options: dict[uuid.UUID, SellingOption],
    existing_lines_by_id: dict[uuid.UUID, OrderLine],
    submitted_ids: set[uuid.UUID],
    payments_total: Decimal,
) -> None:
    """Phase B of the shared, non-committing full-reconciliation core (Phase 7 Final
    Remediation Correction Plan, Finding 1) — reconcile/mutate ONLY, given Phase A's
    (`_prepare_order_edit`) already-locked/validated reference data. Every locked-
    then-rejected path below rolls back before raising (Amendment §2)."""
    # Every domain/calculator ApiError raised past this point (line reconciliation,
    # snapshot representability) happens after the Order row lock and the reference locks
    # already acquired in Phase A — roll back before propagating (Checkpoint-3
    # correction: locked rejections must be universally rolled back).
    try:
        for line_id, existing_line in list(existing_lines_by_id.items()):
            if line_id not in submitted_ids:
                order.lines.remove(existing_line)
                db.delete(existing_line)

        line_subtotals: list[Decimal] = []
        for line in payload.lines:
            existing = existing_lines_by_id.get(line.id) if line.id is not None else None
            columns = _reconcile_line(line, existing, products, selling_options)
            if existing is not None:
                if columns is not None:
                    for key, value in columns.items():
                        setattr(existing, key, value)
                line_subtotals.append(existing.line_subtotal)
            else:
                assert columns is not None
                new_line = OrderLine(
                    id=uuid.uuid4(),
                    business_id=business.id,
                    order_id=order.id,
                    line_type=line.line_type,
                    **columns,
                )
                db.add(new_line)
                line_subtotals.append(new_line.line_subtotal)
    except ApiError:
        db.rollback()
        raise

    order.customer_id = payload.customer_id
    order.fulfillment_date = payload.fulfillment_date
    order.fulfillment_time = payload.fulfillment_time
    order.fulfillment_method = payload.fulfillment_method
    order.fulfillment_details = payload.fulfillment_details
    order.fulfillment_notes = payload.fulfillment_notes
    order.internal_notes = payload.internal_notes
    order.order_adjustment = order_pricing.quantize_money(payload.order_adjustment)
    order.adjustment_description = payload.adjustment_description
    order.manual_tax = order_pricing.quantize_money(payload.manual_tax)

    totals = order_pricing.calculate_order_totals(
        line_subtotals, order_adjustment=order.order_adjustment, manual_tax=order.manual_tax
    )
    new_subtotal = order_pricing.quantize_money(totals.subtotal)
    new_final_total = order_pricing.quantize_money(totals.final_total)
    if not order_pricing.is_representable_in_numeric_14_2(new_subtotal):
        db.rollback()
        raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", "subtotal is too large to store.")
    if new_final_total < 0:
        db.rollback()
        raise ApiError(422, "ORDER_FINAL_TOTAL_NEGATIVE", "The order total cannot be negative.")
    if not order_pricing.is_representable_in_numeric_14_2(new_final_total):
        db.rollback()
        raise ApiError(422, "ORDER_LINE_VALUE_OVERFLOW", "final_total is too large to store.")

    if new_final_total < payments_total and not payload.confirm_overpayment:
        db.rollback()
        raise payment_overage_error(
            existing_total=payments_total, final_total=new_final_total, proposed=Decimal("0")
        )

    order.subtotal = new_subtotal
    order.final_total = new_final_total


def _apply_order_edit(
    db: Session,
    business: Business,
    order: Order,
    payload: OrderUpdateRequest,
    *,
    additional_product_ids: frozenset[uuid.UUID] = frozenset(),
) -> dict[uuid.UUID, Product]:
    """Back-to-back Phase A (`_prepare_order_edit`) + Phase B
    (`_apply_order_edit_body`) — net-identical observable behavior to the original
    combined function, preserved for `update_draft_order` (a DRAFT edit needs no
    operational lock interleaved between validation and mutation). A Phase 7
    confirmed edit instead calls the two phases directly from
    `order_lifecycle_service.update_confirmed_order`, acquiring Recipe/Ingredient/
    Surplus locks between them (Final Architecture Lock §A/§B)."""
    products, selling_options, existing_lines_by_id, submitted_ids, payments_total = (
        _prepare_order_edit(
            db, business, order, payload, additional_product_ids=additional_product_ids
        )
    )
    _apply_order_edit_body(
        db,
        business,
        order,
        payload,
        products=products,
        selling_options=selling_options,
        existing_lines_by_id=existing_lines_by_id,
        submitted_ids=submitted_ids,
        payments_total=payments_total,
    )
    return products


def update_draft_order(
    db: Session, business: Business, order_id: uuid.UUID, payload: OrderUpdateRequest
) -> Order:
    """Thin, DRAFT-only wrapper around `_apply_order_edit` (Phase 7 Implementation
    Remediation Plan, Finding 1) — observable behavior unchanged from before the
    extraction: lock -> version check -> status check -> full reconciliation ->
    touch_order -> single commit."""
    order = get_order_for_business_locked(db, order_id, business)
    try:
        check_version(order, payload.version, resource="order")
    except ApiError:
        db.rollback()
        raise

    if order.status is not OrderStatus.DRAFT:
        db.rollback()
        raise ApiError(409, "ORDER_NOT_DRAFT", "Only a Draft order can be edited.")

    _apply_order_edit(db, business, order, payload)

    touch_order(order)
    commit_or_raise_stale(db, resource="order")
    return order


def delete_draft_order(
    db: Session,
    business: Business,
    order_id: uuid.UUID,
    expected_version: int,
    *,
    confirm_delete_with_payments: bool = False,
) -> None:
    """Backend-enforced warn-then-acknowledge Draft deletion (Final Plan §I/ORD-015) —
    never a frontend-only modal. Relies on the model's already-real
    `cascade="all, delete-orphan", passive_deletes=True` on lines/payments/status_history
    for the atomic cascade."""
    order = get_order_for_business_locked(db, order_id, business)
    try:
        check_version(order, expected_version, resource="order")
    except ApiError:
        db.rollback()
        raise

    if order.status is not OrderStatus.DRAFT:
        db.rollback()
        raise ApiError(409, "ORDER_NOT_DRAFT", "Only a Draft order can be deleted.")

    payments = order.payments
    if payments and not confirm_delete_with_payments:
        # Capture concrete primitive values while the lock is still held and the
        # transaction is still open — never derive warning details from ORM attributes
        # after `db.rollback()` (Checkpoint-3 correction 9): a rollback expires the
        # session's objects, so reading `p.amount` afterward could trigger an
        # inconsistent lazy reload outside the lock rather than reflecting the exact
        # locked state that was evaluated.
        payment_count = len(payments)
        payment_total = sum((p.amount for p in payments), Decimal("0"))
        db.rollback()
        raise ApiError(
            422,
            "ORDER_DELETE_HAS_PAYMENTS_WARNING",
            "This draft has recorded payments.",
            issues=[
                {
                    "severity": "WARNING",
                    "code": "ORDER_HAS_PAYMENTS",
                    "message": "Deleting this draft will also delete its recorded payments.",
                    "field": None,
                    "resource": "order",
                    "details": {
                        "payment_count": payment_count,
                        "payment_total": str(payment_total),
                    },
                }
            ],
        )

    try:
        db.delete(order)
        db.commit()
    except StaleDataError:
        db.rollback()
        raise stale_version_error(resource="order") from None
