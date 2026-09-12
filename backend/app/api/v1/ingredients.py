"""Ingredient CRUD/list/archive/reactivate/delete endpoints (Phase 4 Plan v4 §7).

Thin routers per `CLAUDE.md`/Spec §11.2 — validation and business logic live in
`app.services.ingredient_service`. Every mutating route depends on `require_csrf`
individually (never router-wide), matching the established Customer/Product pattern.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.ingredient import (
    IngredientCreateRequest,
    IngredientResponse,
    IngredientSummary,
    IngredientUpdateRequest,
    LifecycleActionRequest,
)
from app.services import ingredient_service

router = APIRouter()


def _to_response(ingredient: Ingredient) -> IngredientResponse:
    return IngredientResponse(
        id=str(ingredient.id),
        name=ingredient.name,
        measurement_family=ingredient.measurement_family,
        canonical_unit=ingredient.canonical_unit,
        is_active=ingredient.is_active,
        version=ingredient.version,
    )


def _to_summary(ingredient: Ingredient) -> IngredientSummary:
    return IngredientSummary(
        id=str(ingredient.id),
        name=ingredient.name,
        measurement_family=ingredient.measurement_family,
        canonical_unit=ingredient.canonical_unit,
        is_active=ingredient.is_active,
        version=ingredient.version,
    )


@router.get("")
def list_ingredients(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    q: str | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[IngredientSummary]:
    items, total = ingredient_service.list_ingredients_for_business(
        db, business, q=q, is_active=is_active, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_to_summary(i) for i in items], total=total, limit=limit, offset=offset
    )


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_ingredient(
    payload: IngredientCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_service.create_ingredient(db, business, payload)
    return _to_response(ingredient)


@router.get("/{ingredient_id}")
def get_ingredient(
    ingredient_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_service.get_ingredient_for_business(db, ingredient_id, business)
    return _to_response(ingredient)


@router.patch("/{ingredient_id}", dependencies=[Depends(require_csrf)])
def update_ingredient(
    ingredient_id: uuid.UUID,
    payload: IngredientUpdateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_service.update_ingredient(db, business, ingredient_id, payload)
    return _to_response(ingredient)


@router.post("/{ingredient_id}/archive", dependencies=[Depends(require_csrf)])
def archive_ingredient(
    ingredient_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_service.archive_ingredient(db, business, ingredient_id, payload.version)
    return _to_response(ingredient)


@router.post("/{ingredient_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_ingredient(
    ingredient_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_service.reactivate_ingredient(
        db, business, ingredient_id, payload.version
    )
    return _to_response(ingredient)


@router.delete("/{ingredient_id}", status_code=204, dependencies=[Depends(require_csrf)])
def delete_ingredient(
    ingredient_id: uuid.UUID,
    version: int,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    ingredient_service.delete_ingredient(db, business, ingredient_id, version)
