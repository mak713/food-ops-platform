"""Purchased Product Inventory business logic (Phase 5 Plan §C/§E): Initial Balance,
Restock (which, uniquely among Phase 5 mutations, may itself create the inventory row —
Phase 5 Plan approval decision 4), Manual Adjustment, replacement-cost maintenance, and
history — layered on `purchased_product_inventory`/`purchased_product_inventory_transactions`,
already established in Phase 1.

Concurrency: ordinary mutations against an *already-existing* row use the row's own
`version_id_col` alone (now wired, mirroring ADR-104) — no Product lock. The Product row
lock (`product_service.get_product_for_business_locked`, reused, not duplicated) is
acquired *only* for the "does `PurchasedProductInventory` exist yet" check-then-act race,
by whichever of Initial Balance or a version-omitted first Restock gets there first; the
loser's locked re-check always discovers the winner's row and is rejected — never treated
as license to write. The database's own `UNIQUE(business_id, product_id)` constraint is a
backstop, never the primary concurrency mechanism (a narrow `IntegrityError` catch
defends against it as belt-and-suspenders, exactly like `recipe_service.py`'s own
`_RECIPE_IDENTITY_CONSTRAINT` handling)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from psycopg.errors import UniqueViolation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.tenant import check_version, commit_or_raise_stale
from app.db.enums import ProductType, PurchasedInventoryTransactionType
from app.db.models.business import Business
from app.db.models.product import Product
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.domain.inventory_costing import (
    calculate_total_cost,
    calculate_weighted_average_restock,
    is_representable_in_numeric_18_6,
    quantize_for_storage,
    resolve_effective_replacement_cost,
)
from app.schemas.inventory import (
    PurchasedAdjustmentRequest,
    PurchasedInitialBalanceRequest,
    PurchasedRestockRequest,
    ReplacementCostRequest,
)
from app.services.product_service import get_product_for_business, get_product_for_business_locked

_INVENTORY_IDENTITY_CONSTRAINT = "uq_purchased_product_inventory_business_id_product_id"


def effective_replacement_cost(inventory: PurchasedProductInventory) -> Decimal | None:
    return resolve_effective_replacement_cost(
        inventory.replacement_unit_cost, inventory.latest_purchase_unit_cost
    )


def _require_purchased_type(product: Product) -> None:
    if product.product_type is not ProductType.PURCHASED:
        raise ApiError(
            422,
            "PURCHASED_INVENTORY_REQUIRES_PURCHASED_PRODUCT",
            "Only Purchased products can have purchased-inventory tracking.",
        )


def _require_representable(value: Decimal, *, field: str) -> None:
    """Phase 5 correction-pass finding 4: Purchased Product Inventory has no unit
    conversion, but a derived value can still exceed `NUMERIC(18,6)` — adding two
    individually-valid balances together (restock/adjustment against an already-large
    `physical_quantity`), or multiplying two individually-valid Decimals together
    (`quantity * unit_cost` for `total_cost`)."""
    if not is_representable_in_numeric_18_6(value):
        raise ApiError(
            422,
            "VALUE_OUT_OF_RANGE",
            f"The computed {field} is too large to be stored.",
        )


def _require_active(product: Product, *, action: str) -> None:
    if not product.is_active:
        raise ApiError(
            409,
            "PURCHASED_INVENTORY_PRODUCT_INACTIVE",
            f"This product is inactive. Reactivate it before {action}.",
        )


def _fetch_inventory_row(
    db: Session, business: Business, product: Product
) -> PurchasedProductInventory | None:
    """Plain, unlocked, tenant/product-scoped `SELECT` — never `.with_for_update()`. Used
    by every read and by the pre-lock existence checks in the mutation paths below."""
    return db.scalar(
        select(PurchasedProductInventory).where(
            PurchasedProductInventory.product_id == product.id,
            PurchasedProductInventory.business_id == business.id,
        )
    )


def _state_changed_error() -> ApiError:
    return ApiError(
        409,
        "PURCHASED_INVENTORY_STATE_CHANGED",
        "This product's inventory changed since you last checked. Refresh and resubmit "
        "with the current inventory version.",
    )


def get_purchased_inventory(
    db: Session, business: Business, product_id: uuid.UUID
) -> PurchasedProductInventory:
    """404s (via a dedicated code, not the generic tenant-lookup `NotFoundError`) if the
    Product exists but its inventory has never been initialized yet — mirrors Recipe's
    "no recipe yet" 404 shape. Allowed regardless of the Product's active state (Phase 5
    Plan approval decision 6 — reads remain allowed)."""
    product = get_product_for_business(db, product_id, business)
    _require_purchased_type(product)
    row = _fetch_inventory_row(db, business, product)
    if row is None:
        raise ApiError(
            404,
            "PURCHASED_INVENTORY_NOT_INITIALIZED",
            "This product's inventory has not been initialized yet.",
        )
    return row


def create_initial_balance(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    payload: PurchasedInitialBalanceRequest,
) -> PurchasedProductInventory:
    """Always locks the Product row first — Initial Balance always attempts a fresh
    creation, so it must serialize against any other creation attempt (a second Initial
    Balance, or a version-omitted first Restock) via the shared-ancestor lock (ADR-106),
    exactly as `create_recipe` locks the Product before checking Recipe-nonexistence."""
    product = get_product_for_business_locked(db, product_id, business)
    try:
        _require_purchased_type(product)
        existing = _fetch_inventory_row(db, business, product)
        if existing is not None:
            raise ApiError(
                409,
                "PURCHASED_INVENTORY_ALREADY_INITIALIZED",
                "This product's inventory has already been initialized. Use Restock or "
                "Manual Adjustment instead.",
            )
        _require_active(product, action="recording an initial balance")

        normalized_qty = quantize_for_storage(payload.quantity)
        normalized_cost = quantize_for_storage(payload.unit_cost)
        total_cost = quantize_for_storage(calculate_total_cost(normalized_qty, normalized_cost))
        _require_representable(total_cost, field="total cost")
    except ApiError:
        db.rollback()
        raise

    inventory = PurchasedProductInventory(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        physical_quantity=normalized_qty,
        weighted_average_unit_cost=normalized_cost,
        # Not evidence of an actual purchase event (approval decision 3) — Latest
        # Purchase Cost and the explicit Replacement Cost override both stay NULL.
    )
    db.add(inventory)
    db.add(
        PurchasedProductInventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            product_id=product.id,
            transaction_type=PurchasedInventoryTransactionType.INITIAL_BALANCE,
            quantity_change=normalized_qty,
            unit_cost=normalized_cost,
            total_cost=total_cost,
            supplier_text=payload.supplier_text,
            notes=payload.notes,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == _INVENTORY_IDENTITY_CONSTRAINT
        ):
            # Defense-in-depth only: the Product lock above already makes this race-free
            # in normal operation.
            raise ApiError(
                409,
                "PURCHASED_INVENTORY_ALREADY_INITIALIZED",
                "This product's inventory has already been initialized. Use Restock or "
                "Manual Adjustment instead.",
            ) from None
        raise
    return inventory


def restock_purchased_product(
    db: Session, business: Business, product_id: uuid.UUID, payload: PurchasedRestockRequest
) -> PurchasedProductInventory:
    """A first-ever Restock (no prior Initial Balance) is legitimate (approval decision
    4) — `payload.version is None` asserts "I believe no inventory row exists yet."

    Race walkthrough (all four required combinations):
    - **First-Restock vs. first-Restock**: both submit `version=None`; both see no row on
      their pre-lock check; both attempt `get_product_for_business_locked`. The winner's
      locked re-check finds nothing, creates the row + `RESTOCK` transaction, commits.
      The loser, unblocked after, re-checks under its own lock, finds the winner's row,
      and receives `409 PURCHASED_INVENTORY_STATE_CHANGED` — never silently applying
      itself afterward.
    - **Initial-Balance vs. first-Restock**: both lock the same Product row (Initial
      Balance always locks; first-Restock locks only on the `version=None` path) — same
      outcome as above, symmetric regardless of which wins.
    - **Restock vs. Restock on an existing row**: both submit a concrete `version`; no
      lock is taken; ordinary `version_id_col` optimistic concurrency protects the
      update — one succeeds, the other gets `409 STALE_VERSION` via
      `commit_or_raise_stale`.
    - **A `version=None` Restock arriving after the row already exists**: the pre-lock
      check finds the row and rejects immediately with `409
      PURCHASED_INVENTORY_STATE_CHANGED`, directing the caller to resubmit with the
      current version — it never reaches the lock/creation path at all.
    """
    product = get_product_for_business(db, product_id, business)
    _require_purchased_type(product)
    existing = _fetch_inventory_row(db, business, product)

    if payload.version is not None:
        # Existing-row path — the caller asserts a row it already knows the version of.
        if existing is None:
            raise _state_changed_error()
        _require_active(product, action="restocking")
        check_version(existing, payload.version, resource="purchased_product_inventory")
        return _apply_restock_to_existing_row(db, business, existing, payload)

    # version is None — the caller asserts "no row exists yet."
    if existing is not None:
        raise _state_changed_error()
    _require_active(product, action="restocking")

    locked_product = get_product_for_business_locked(db, product_id, business)
    row_under_lock = _fetch_inventory_row(db, business, locked_product)
    if row_under_lock is not None:
        db.rollback()
        raise _state_changed_error()

    normalized_qty = quantize_for_storage(payload.quantity)
    normalized_cost = quantize_for_storage(payload.unit_cost)
    total_cost = quantize_for_storage(calculate_total_cost(normalized_qty, normalized_cost))
    try:
        _require_representable(total_cost, field="total cost")
    except ApiError:
        db.rollback()
        raise

    inventory = PurchasedProductInventory(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=locked_product.id,
        physical_quantity=normalized_qty,
        weighted_average_unit_cost=normalized_cost,
        latest_purchase_unit_cost=normalized_cost,
        # `replacement_unit_cost` stays NULL — never set by a restock (decision 5).
    )
    db.add(inventory)
    db.add(
        PurchasedProductInventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            product_id=locked_product.id,
            transaction_type=PurchasedInventoryTransactionType.RESTOCK,
            quantity_change=normalized_qty,
            unit_cost=normalized_cost,
            total_cost=total_cost,
            supplier_text=payload.supplier_text,
            notes=payload.notes,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == _INVENTORY_IDENTITY_CONSTRAINT
        ):
            raise _state_changed_error() from None
        raise
    return inventory


def _apply_restock_to_existing_row(
    db: Session,
    business: Business,
    inventory: PurchasedProductInventory,
    payload: PurchasedRestockRequest,
) -> PurchasedProductInventory:
    normalized_qty = quantize_for_storage(payload.quantity)
    normalized_cost = quantize_for_storage(payload.unit_cost)
    new_weighted_average = quantize_for_storage(
        calculate_weighted_average_restock(
            old_quantity=inventory.physical_quantity,
            old_weighted_average=inventory.weighted_average_unit_cost,
            purchased_quantity=normalized_qty,
            purchase_unit_cost=normalized_cost,
        )
    )
    _require_representable(new_weighted_average, field="weighted-average unit cost")
    total_cost = quantize_for_storage(calculate_total_cost(normalized_qty, normalized_cost))
    _require_representable(total_cost, field="total cost")

    new_physical_quantity = inventory.physical_quantity + normalized_qty
    _require_representable(new_physical_quantity, field="physical quantity")

    inventory.physical_quantity = new_physical_quantity
    inventory.weighted_average_unit_cost = new_weighted_average
    inventory.latest_purchase_unit_cost = normalized_cost

    db.add(
        PurchasedProductInventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            product_id=inventory.product_id,
            transaction_type=PurchasedInventoryTransactionType.RESTOCK,
            quantity_change=normalized_qty,
            unit_cost=normalized_cost,
            total_cost=total_cost,
            supplier_text=payload.supplier_text,
            notes=payload.notes,
        )
    )
    commit_or_raise_stale(db, resource="purchased_product_inventory")
    return inventory


def adjust_purchased_product(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    payload: PurchasedAdjustmentRequest,
) -> PurchasedProductInventory:
    """Allowed on an inactive Product (reconciliation of remaining recorded stock,
    approval decision 6). Purchased Product Inventory's `physical_quantity` carries a
    hard DB `CHECK (>= 0)` — this pre-validates that same condition to produce a clean
    `422` before the CHECK could ever fire (ADR-103-style narrow-IntegrityError
    discipline: the pre-check is the primary error surface, the CHECK is the backstop)."""
    product = get_product_for_business(db, product_id, business)
    _require_purchased_type(product)
    inventory = _fetch_inventory_row(db, business, product)
    if inventory is None:
        raise ApiError(
            404,
            "PURCHASED_INVENTORY_NOT_INITIALIZED",
            "This product's inventory has not been initialized yet.",
        )
    check_version(inventory, payload.version, resource="purchased_product_inventory")

    new_quantity = inventory.physical_quantity + payload.quantity_change
    _require_representable(new_quantity, field="physical quantity")
    if new_quantity < 0:
        raise ApiError(
            422,
            "PURCHASED_INVENTORY_INSUFFICIENT_BALANCE",
            "This adjustment would make the purchased inventory balance negative, which "
            "is not permitted.",
        )

    inventory.physical_quantity = new_quantity
    db.add(
        PurchasedProductInventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            product_id=product.id,
            transaction_type=PurchasedInventoryTransactionType.MANUAL_ADJUSTMENT,
            quantity_change=payload.quantity_change,
            reason=payload.reason.value,
            notes=payload.notes,
        )
    )
    commit_or_raise_stale(db, resource="purchased_product_inventory")
    return inventory


def set_replacement_cost(
    db: Session, business: Business, product_id: uuid.UUID, payload: ReplacementCostRequest
) -> PurchasedProductInventory:
    product = get_product_for_business(db, product_id, business)
    _require_purchased_type(product)
    inventory = _fetch_inventory_row(db, business, product)
    if inventory is None:
        raise ApiError(
            404,
            "PURCHASED_INVENTORY_NOT_INITIALIZED",
            "This product's inventory has not been initialized yet.",
        )
    _require_active(product, action="maintaining a replacement-cost override")
    check_version(inventory, payload.version, resource="purchased_product_inventory")

    inventory.replacement_unit_cost = payload.replacement_unit_cost
    commit_or_raise_stale(db, resource="purchased_product_inventory")
    return inventory


def list_purchased_inventory_transactions(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PurchasedProductInventoryTransaction], int]:
    """Read-only, allowed regardless of the Product's active state."""
    product = get_product_for_business(db, product_id, business)
    _require_purchased_type(product)
    conditions = [
        PurchasedProductInventoryTransaction.product_id == product.id,
        PurchasedProductInventoryTransaction.business_id == business.id,
    ]
    total = (
        db.scalar(
            select(func.count())
            .select_from(PurchasedProductInventoryTransaction)
            .where(*conditions)
        )
        or 0
    )
    items = list(
        db.scalars(
            select(PurchasedProductInventoryTransaction)
            .where(*conditions)
            .order_by(PurchasedProductInventoryTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def list_purchased_inventory_for_business(
    db: Session,
    business: Business,
    *,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[Product, PurchasedProductInventory | None]], int]:
    """Every `PURCHASED` Product for the tenant, `LEFT JOIN`ed against its inventory row
    (which may not exist yet) — powers the top-level Inventory-module list (Phase 5 Plan
    §F). Includes inactive Products by default (`is_active=None`), consistent with the
    existing archive-filter convention already used by `list_ingredients_for_business`/
    `list_products_for_business`, and per approval decision 6/8 — history/current-balance
    reads must remain reachable for an inactive Product."""
    conditions = [Product.business_id == business.id, Product.product_type == ProductType.PURCHASED]
    if is_active is not None:
        conditions.append(Product.is_active == is_active)

    total = db.scalar(select(func.count()).select_from(Product).where(*conditions)) or 0
    rows = db.execute(
        select(Product, PurchasedProductInventory)
        .outerjoin(
            PurchasedProductInventory,
            (PurchasedProductInventory.product_id == Product.id)
            & (PurchasedProductInventory.business_id == Product.business_id),
        )
        .where(*conditions)
        .order_by(Product.name.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [(row[0], row[1]) for row in rows]
    return items, total
