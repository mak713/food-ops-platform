"""Ingredient physical-inventory business logic (Phase 5 Plan §C/§D): Initial Balance,
Restock, Manual Adjustment, replacement-cost maintenance, and history — layered on the
Ingredient columns and `inventory_transactions` ledger already established in Phase 1.

Every mutation is a single-commit, fetch/lock -> validate -> mutate current-state row in
memory -> insert one matching `InventoryTransaction` -> commit sequence, mirroring
`recipe_service.py`'s transactional discipline exactly. Concurrency: ordinary mutations
(Restock, Adjustment, replacement-cost) rely solely on Ingredient's existing
`version_id_col` mechanism (ADR-104) — no row lock, since the Ingredient row always
already exists (created in Phase 4). Only `create_initial_balance` acquires
`get_ingredient_for_business_locked`'s `FOR UPDATE`, to serialize the "has this Ingredient
ever been initialized" check-then-act race against a concurrent second Initial Balance
call — see that function's docstring for the full race walkthrough.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.tenant import check_version, commit_or_raise_stale
from app.db.enums import InventoryTransactionType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient, InventoryTransaction
from app.domain.inventory_costing import (
    calculate_total_cost,
    calculate_weighted_average_restock,
    is_representable_in_numeric_18_6,
    quantize_for_storage,
    resolve_effective_replacement_cost,
)
from app.domain.unit_conversion import UnknownUnitError, convert, convert_unit_cost, unit_family
from app.schemas.inventory import (
    IngredientAdjustmentRequest,
    IngredientInitialBalanceRequest,
    IngredientRestockRequest,
    ReplacementCostRequest,
)
from app.services.ingredient_service import (
    get_ingredient_for_business,
    get_ingredient_for_business_locked,
)


def effective_replacement_cost(ingredient: Ingredient) -> Decimal | None:
    return resolve_effective_replacement_cost(
        ingredient.replacement_unit_cost, ingredient.latest_purchase_unit_cost
    )


def _has_any_inventory_transaction(db: Session, ingredient: Ingredient) -> bool:
    return (
        db.scalar(
            select(InventoryTransaction.id)
            .where(InventoryTransaction.ingredient_id == ingredient.id)
            .limit(1)
        )
        is not None
    )


def _require_active(ingredient: Ingredient, *, action: str) -> None:
    if not ingredient.is_active:
        raise ApiError(
            409,
            "INGREDIENT_INVENTORY_ARCHIVED",
            f"This ingredient is archived. Reactivate it before {action}.",
        )


def _require_representable(value: Decimal, *, field: str) -> None:
    """Phase 5 correction-pass finding 4: a *derived* value (a unit-converted quantity/
    cost, a post-arithmetic balance, a computed total cost) can exceed what
    `NUMERIC(18,6)` can store even though every raw request field is individually
    within range — reject cleanly here rather than letting the database's own CHECK/
    numeric-overflow error (or a raw domain `ValueError`) leak through as a 500."""
    if not is_representable_in_numeric_18_6(value):
        raise ApiError(
            422,
            "VALUE_OUT_OF_RANGE",
            f"The computed {field} is too large to be stored.",
        )


def _require_positive_quantity_survived_rounding(
    original: Decimal, quantized: Decimal, *, field: str
) -> None:
    """A raw request quantity is always `> 0` (Pydantic `gt=0`), but converting it to the
    Ingredient's canonical unit can shrink it enough that rounding to 6 decimal places
    (`quantize_for_storage`) collapses it to exactly zero — e.g. entering `0.000001` in a
    small unit against a canonical unit many orders of magnitude larger. A quantity that
    was genuinely positive on entry must never silently become a zero-quantity ledger
    row (Phase 5 correction-pass finding 4, item 1)."""
    if original > 0 and quantized == 0:
        raise ApiError(
            422,
            "QUANTITY_TOO_SMALL",
            f"This {field} is too small to be represented at the required precision "
            "once converted to the ingredient's canonical unit.",
        )


def _validate_unit_or_raise(ingredient: Ingredient, unit: str) -> None:
    """Raises a clean `422` for an unrecognized unit or one belonging to a different
    measurement family than the Ingredient's own — checked via `unit_family` *before*
    calling `convert`/`convert_unit_cost`, so those never raise `UnknownUnitError`/
    `IncompatibleUnitFamilyError` here (mirrors `recipe_service.py`'s own
    check-before-convert discipline, rather than catching those `ValueError` subclasses
    after the fact)."""
    try:
        family = unit_family(unit)
    except UnknownUnitError as exc:
        raise ApiError(422, "INVALID_UNIT", f"{unit!r} is not a recognized unit.") from exc
    if family is not ingredient.measurement_family:
        raise ApiError(
            422,
            "CROSS_FAMILY_UNIT_CONVERSION",
            f"{unit!r} is not compatible with this ingredient's measurement family.",
        )


def create_initial_balance(
    db: Session,
    business: Business,
    ingredient_id: uuid.UUID,
    payload: IngredientInitialBalanceRequest,
) -> Ingredient:
    """One-time-only (Phase 5 Plan approval decision 3). Locks the Ingredient row first,
    then checks the caller's expected `version` (a genuine stale-version conflict is
    reported before the already-initialized check, matching the order the approval
    specifies), then re-checks under that same lock whether any `InventoryTransaction`
    already exists for it.

    Race walkthrough — two concurrent Initial-Balance calls on the same Ingredient: T1
    acquires the row lock first, finds no prior transaction, initializes, commits,
    releases the lock. T2, unblocked only after T1 commits, re-checks under its own lock
    and finds the transaction T1 just inserted -> `409
    INGREDIENT_INVENTORY_ALREADY_INITIALIZED`, rollback, no write. Race walkthrough — a
    concurrent Restock arrives mid-flight: T2's plain unlocked read sees the pre-T1 state
    and later attempts `UPDATE ... WHERE version = <stale>`, which blocks behind T1's
    still-held lock and, once unblocked, matches zero rows (T1 already bumped `version`) ->
    `StaleDataError` -> `409 STALE_VERSION` for T2. No special-case code is needed for
    either interleaving — it falls out of combining the lock with the existing
    `version_id_col` mechanism."""
    ingredient = get_ingredient_for_business_locked(db, ingredient_id, business)
    try:
        check_version(ingredient, payload.version, resource="ingredient")
        _require_active(ingredient, action="recording an initial balance")
        _validate_unit_or_raise(ingredient, payload.unit)
        if _has_any_inventory_transaction(db, ingredient):
            raise ApiError(
                409,
                "INGREDIENT_INVENTORY_ALREADY_INITIALIZED",
                "This ingredient's inventory has already been initialized. Use Restock "
                "or Manual Adjustment instead.",
            )

        normalized_qty = quantize_for_storage(
            convert(payload.quantity, payload.unit, ingredient.canonical_unit)
        )
        _require_positive_quantity_survived_rounding(
            payload.quantity, normalized_qty, field="quantity"
        )
        _require_representable(normalized_qty, field="quantity")
        normalized_cost = quantize_for_storage(
            convert_unit_cost(payload.unit_cost, payload.unit, ingredient.canonical_unit)
        )
        _require_representable(normalized_cost, field="unit cost")
        total_cost = quantize_for_storage(calculate_total_cost(normalized_qty, normalized_cost))
        _require_representable(total_cost, field="total cost")
    except ApiError:
        db.rollback()
        raise

    ingredient.physical_quantity = normalized_qty
    ingredient.weighted_average_unit_cost = normalized_cost
    # Not evidence of an actual purchase event (Phase 5 Plan approval decision 3) —
    # Latest Purchase Cost and the explicit Replacement Cost override both stay NULL.

    db.add(
        InventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            ingredient_id=ingredient.id,
            transaction_type=InventoryTransactionType.INITIAL_BALANCE,
            quantity_change=normalized_qty,
            unit_cost=normalized_cost,
            total_cost=total_cost,
            supplier_text=payload.supplier_text,
            notes=payload.notes,
        )
    )
    commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def restock_ingredient(
    db: Session, business: Business, ingredient_id: uuid.UUID, payload: IngredientRestockRequest
) -> Ingredient:
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, payload.version, resource="ingredient")
    _require_active(ingredient, action="restocking")
    _validate_unit_or_raise(ingredient, payload.unit)

    normalized_qty = quantize_for_storage(
        convert(payload.quantity, payload.unit, ingredient.canonical_unit)
    )
    _require_positive_quantity_survived_rounding(payload.quantity, normalized_qty, field="quantity")
    _require_representable(normalized_qty, field="quantity")
    normalized_cost = quantize_for_storage(
        convert_unit_cost(payload.unit_cost, payload.unit, ingredient.canonical_unit)
    )
    _require_representable(normalized_cost, field="unit cost")
    new_weighted_average = quantize_for_storage(
        calculate_weighted_average_restock(
            old_quantity=ingredient.physical_quantity,
            old_weighted_average=ingredient.weighted_average_unit_cost,
            purchased_quantity=normalized_qty,
            purchase_unit_cost=normalized_cost,
        )
    )
    _require_representable(new_weighted_average, field="weighted-average unit cost")
    total_cost = quantize_for_storage(calculate_total_cost(normalized_qty, normalized_cost))
    _require_representable(total_cost, field="total cost")

    new_physical_quantity = ingredient.physical_quantity + normalized_qty
    _require_representable(new_physical_quantity, field="physical quantity")

    ingredient.physical_quantity = new_physical_quantity
    ingredient.weighted_average_unit_cost = new_weighted_average
    ingredient.latest_purchase_unit_cost = normalized_cost
    # `replacement_unit_cost` is never touched by a restock (approval decision 5).

    db.add(
        InventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            ingredient_id=ingredient.id,
            transaction_type=InventoryTransactionType.RESTOCK,
            quantity_change=normalized_qty,
            unit_cost=normalized_cost,
            total_cost=total_cost,
            supplier_text=payload.supplier_text,
            notes=payload.notes,
        )
    )
    commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def adjust_ingredient(
    db: Session,
    business: Business,
    ingredient_id: uuid.UUID,
    payload: IngredientAdjustmentRequest,
) -> Ingredient:
    """Allowed on an archived Ingredient (reconciliation of remaining recorded stock,
    Phase 5 Plan approval decision 6) and may legitimately drive `physical_quantity`
    negative (approval decision 2) — no cost field is ever touched, since a manual
    adjustment carries no cost basis of its own (Spec §15.7)."""
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, payload.version, resource="ingredient")

    new_physical_quantity = ingredient.physical_quantity + payload.quantity_change
    _require_representable(new_physical_quantity, field="physical quantity")

    ingredient.physical_quantity = new_physical_quantity

    db.add(
        InventoryTransaction(
            id=uuid.uuid4(),
            business_id=business.id,
            ingredient_id=ingredient.id,
            transaction_type=InventoryTransactionType.MANUAL_ADJUSTMENT,
            quantity_change=payload.quantity_change,
            reason=payload.reason.value,
            notes=payload.notes,
        )
    )
    commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def set_replacement_cost(
    db: Session, business: Business, ingredient_id: uuid.UUID, payload: ReplacementCostRequest
) -> Ingredient:
    """No `InventoryTransaction` is created — this is a pure current-state metadata
    change, not a physical/cost-basis event (Phase 5 Plan approval decision 5); none of
    the four ledger transaction types describes a replacement-cost change."""
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, payload.version, resource="ingredient")
    _require_active(ingredient, action="maintaining a replacement-cost override")

    ingredient.replacement_unit_cost = payload.replacement_unit_cost
    commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def list_inventory_transactions(
    db: Session,
    business: Business,
    ingredient_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InventoryTransaction], int]:
    """Read-only, allowed regardless of the Ingredient's active state (Phase 5 Plan
    approval decision 6 — history remains readable for an archived resource)."""
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    conditions = [
        InventoryTransaction.ingredient_id == ingredient.id,
        InventoryTransaction.business_id == business.id,
    ]
    total = (
        db.scalar(select(func.count()).select_from(InventoryTransaction).where(*conditions)) or 0
    )
    items = list(
        db.scalars(
            select(InventoryTransaction)
            .where(*conditions)
            .order_by(InventoryTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total
