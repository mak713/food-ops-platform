"""Ingredient CRUD, archive/reactivate/safe-delete business logic (Phase 4 Plan v4 §4).

Mirrors `customer_service.py`'s conventions exactly, minus duplicate-warning (ADR-102: not
extended to Ingredient in Phase 4) and minus any cost/quantity field (Phase 5 scope).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from psycopg.errors import ForeignKeyViolation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.api_errors import ApiError
from app.core.tenant import (
    check_version,
    commit_or_raise_stale,
    get_owned_or_404,
    stale_version_error,
)
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.schemas.ingredient import IngredientCreateRequest, IngredientUpdateRequest


def get_ingredient_for_business(
    db: Session, ingredient_id: uuid.UUID, business: Business
) -> Ingredient:
    return get_owned_or_404(db, Ingredient, ingredient_id, business)


def lock_ingredients_for_business(
    db: Session, ingredient_ids: Iterable[uuid.UUID], business: Business
) -> dict[uuid.UUID, Ingredient]:
    """Resolves and row-locks every distinct Ingredient among `ingredient_ids`, scoped to
    `business`, in one deterministically-ordered query (Phase 4 Plan v4 §6b/§6d) — used
    while validating Recipe/RecipeRevision ingredient-line composition, so a concurrent
    archive/delete of one of these same Ingredients cannot race between validation and
    commit (an `UPDATE`/`DELETE` on a locked row blocks until this transaction ends). IDs
    are sorted before the query so any two operations locking overlapping Ingredient sets
    always request their locks in the same relative order, bounding deadlock risk. An id
    absent from the returned dict is either nonexistent or belongs to another tenant —
    both cases are indistinguishable and are the caller's responsibility to treat
    identically (Phase 4 Plan v4 §9)."""
    ordered_ids = sorted(set(ingredient_ids))
    if not ordered_ids:
        return {}
    rows = db.scalars(
        select(Ingredient)
        .where(Ingredient.id.in_(ordered_ids), Ingredient.business_id == business.id)
        .order_by(Ingredient.id)
        .with_for_update()
    ).all()
    return {row.id: row for row in rows}


def create_ingredient(
    db: Session, business: Business, payload: IngredientCreateRequest
) -> Ingredient:
    ingredient = Ingredient(
        id=uuid.uuid4(),
        business_id=business.id,
        name=payload.name,
        measurement_family=payload.measurement_family,
        canonical_unit=payload.canonical_unit,
    )
    db.add(ingredient)
    db.commit()
    return ingredient


def list_ingredients_for_business(
    db: Session,
    business: Business,
    *,
    q: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Ingredient], int]:
    conditions = [Ingredient.business_id == business.id]
    if q:
        conditions.append(Ingredient.name.ilike(f"%{q}%"))
    if is_active is not None:
        conditions.append(Ingredient.is_active == is_active)

    total = db.scalar(select(func.count()).select_from(Ingredient).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Ingredient)
            .where(*conditions)
            .order_by(Ingredient.name.asc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def update_ingredient(
    db: Session, business: Business, ingredient_id: uuid.UUID, payload: IngredientUpdateRequest
) -> Ingredient:
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, payload.version, resource="ingredient")

    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    for field, value in changes.items():
        setattr(ingredient, field, value)
    if changes:
        commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def archive_ingredient(
    db: Session, business: Business, ingredient_id: uuid.UUID, expected_version: int
) -> Ingredient:
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, expected_version, resource="ingredient")
    if ingredient.is_active:
        ingredient.is_active = False
        commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def reactivate_ingredient(
    db: Session, business: Business, ingredient_id: uuid.UUID, expected_version: int
) -> Ingredient:
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, expected_version, resource="ingredient")
    if not ingredient.is_active:
        ingredient.is_active = True
        commit_or_raise_stale(db, resource="ingredient")
    return ingredient


def delete_ingredient(
    db: Session, business: Business, ingredient_id: uuid.UUID, expected_version: int
) -> None:
    """Safe-delete (Spec §8.32/INV-010; Phase 4 Plan v4 §4): attempt the delete and
    translate only a genuine foreign-key violation into a 409 — an unreferenced Ingredient
    is hard-deletable; a referenced one (recipe lines, inventory transactions, production
    requirements, reservations — all `ondelete="NO ACTION"`) is not, and must be archived
    instead. Any other IntegrityError is re-raised for the existing generic error handler,
    matching the ADR-103 narrowing discipline exactly."""
    ingredient = get_ingredient_for_business(db, ingredient_id, business)
    check_version(ingredient, expected_version, resource="ingredient")

    try:
        db.delete(ingredient)
        db.commit()
    except StaleDataError:
        db.rollback()
        raise stale_version_error(resource="ingredient") from None
    except IntegrityError as exc:
        db.rollback()
        if isinstance(exc.orig, ForeignKeyViolation):
            raise ApiError(
                409,
                "INGREDIENT_HAS_REFERENCES",
                "This ingredient is used elsewhere and cannot be deleted. Archive it instead.",
            ) from None
        raise
