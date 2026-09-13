"""Ingredient physical-inventory endpoints (Phase 5 Plan §C) — mounted at the same
`/api/v1/ingredients` prefix as `app.api.v1.ingredients`'s router. Thin routers per
`CLAUDE.md`/Spec §11.2: validation and business logic live in
`app.services.ingredient_inventory_service`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.api.v1.ingredients import ingredient_to_response
from app.db.models.business import Business
from app.db.models.ingredient import InventoryTransaction
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.ingredient import IngredientResponse
from app.schemas.inventory import (
    IngredientAdjustmentRequest,
    IngredientInitialBalanceRequest,
    IngredientRestockRequest,
    InventoryTransactionResponse,
    ReplacementCostRequest,
)
from app.services import ingredient_inventory_service

router = APIRouter()


def _transaction_to_response(txn: InventoryTransaction) -> InventoryTransactionResponse:
    return InventoryTransactionResponse(
        id=str(txn.id),
        transaction_type=txn.transaction_type.value,
        quantity_change=txn.quantity_change,
        unit_cost=txn.unit_cost,
        total_cost=txn.total_cost,
        supplier_text=txn.supplier_text,
        reason=txn.reason,
        notes=txn.notes,
        created_at=txn.created_at,
    )


@router.post(
    "/{ingredient_id}/inventory/initial-balance",
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
def create_initial_balance(
    ingredient_id: uuid.UUID,
    payload: IngredientInitialBalanceRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_inventory_service.create_initial_balance(
        db, business, ingredient_id, payload
    )
    return ingredient_to_response(ingredient)


@router.post("/{ingredient_id}/inventory/restock", dependencies=[Depends(require_csrf)])
def restock_ingredient(
    ingredient_id: uuid.UUID,
    payload: IngredientRestockRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_inventory_service.restock_ingredient(
        db, business, ingredient_id, payload
    )
    return ingredient_to_response(ingredient)


@router.post("/{ingredient_id}/inventory/adjustments", dependencies=[Depends(require_csrf)])
def adjust_ingredient(
    ingredient_id: uuid.UUID,
    payload: IngredientAdjustmentRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_inventory_service.adjust_ingredient(
        db, business, ingredient_id, payload
    )
    return ingredient_to_response(ingredient)


@router.put("/{ingredient_id}/inventory/replacement-cost", dependencies=[Depends(require_csrf)])
def set_replacement_cost(
    ingredient_id: uuid.UUID,
    payload: ReplacementCostRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> IngredientResponse:
    ingredient = ingredient_inventory_service.set_replacement_cost(
        db, business, ingredient_id, payload
    )
    return ingredient_to_response(ingredient)


@router.get("/{ingredient_id}/inventory/transactions")
def list_inventory_transactions(
    ingredient_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[InventoryTransactionResponse]:
    items, total = ingredient_inventory_service.list_inventory_transactions(
        db, business, ingredient_id, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_transaction_to_response(t) for t in items], total=total, limit=limit, offset=offset
    )
