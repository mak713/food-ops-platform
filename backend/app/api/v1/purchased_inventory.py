"""Purchased Product Inventory endpoints (Phase 5 Plan §C/§E). `router` is mounted at the
same `/api/v1/products` prefix as `app.api.v1.products`'s router (nested-under-Product
shape, matching Recipe); `list_router` is mounted at its own top-level
`/api/v1/purchased-inventory` prefix, powering the Inventory-module list screen that must
not depend on Product-detail nesting alone (Phase 5 Plan §F). Thin routers per
`CLAUDE.md`/Spec §11.2 — validation and business logic live in
`app.services.purchased_inventory_service`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.db.models.business import Business
from app.db.models.product import Product
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.inventory import (
    InventoryTransactionResponse,
    PurchasedAdjustmentRequest,
    PurchasedInitialBalanceRequest,
    PurchasedProductInventoryResponse,
    PurchasedProductInventorySummary,
    PurchasedRestockRequest,
    ReplacementCostRequest,
)
from app.services import purchased_inventory_service

router = APIRouter()
list_router = APIRouter()


def _inventory_to_response(
    inventory: PurchasedProductInventory,
) -> PurchasedProductInventoryResponse:
    return PurchasedProductInventoryResponse(
        product_id=str(inventory.product_id),
        physical_quantity=inventory.physical_quantity,
        weighted_average_unit_cost=inventory.weighted_average_unit_cost,
        latest_purchase_unit_cost=inventory.latest_purchase_unit_cost,
        replacement_unit_cost=inventory.replacement_unit_cost,
        effective_replacement_cost=purchased_inventory_service.effective_replacement_cost(
            inventory
        ),
        version=inventory.version,
    )


def _transaction_to_response(
    txn: PurchasedProductInventoryTransaction,
) -> InventoryTransactionResponse:
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


def _summary_to_response(
    product: Product, inventory: PurchasedProductInventory | None
) -> PurchasedProductInventorySummary:
    return PurchasedProductInventorySummary(
        product_id=str(product.id),
        product_name=product.name,
        product_is_active=product.is_active,
        physical_quantity=inventory.physical_quantity if inventory else None,
        weighted_average_unit_cost=inventory.weighted_average_unit_cost if inventory else None,
        latest_purchase_unit_cost=inventory.latest_purchase_unit_cost if inventory else None,
        replacement_unit_cost=inventory.replacement_unit_cost if inventory else None,
        effective_replacement_cost=(
            purchased_inventory_service.effective_replacement_cost(inventory) if inventory else None
        ),
        version=inventory.version if inventory else None,
    )


# --- Nested under Product (mounted at /api/v1/products) -------------------------------


@router.get("/{product_id}/purchased-inventory")
def get_purchased_inventory(
    product_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> PurchasedProductInventoryResponse:
    inventory = purchased_inventory_service.get_purchased_inventory(db, business, product_id)
    return _inventory_to_response(inventory)


@router.post(
    "/{product_id}/purchased-inventory/initial-balance",
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
def create_initial_balance(
    product_id: uuid.UUID,
    payload: PurchasedInitialBalanceRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> PurchasedProductInventoryResponse:
    inventory = purchased_inventory_service.create_initial_balance(
        db, business, product_id, payload
    )
    return _inventory_to_response(inventory)


@router.post("/{product_id}/purchased-inventory/restock", dependencies=[Depends(require_csrf)])
def restock_purchased_product(
    product_id: uuid.UUID,
    payload: PurchasedRestockRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> PurchasedProductInventoryResponse:
    inventory = purchased_inventory_service.restock_purchased_product(
        db, business, product_id, payload
    )
    return _inventory_to_response(inventory)


@router.post("/{product_id}/purchased-inventory/adjustments", dependencies=[Depends(require_csrf)])
def adjust_purchased_product(
    product_id: uuid.UUID,
    payload: PurchasedAdjustmentRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> PurchasedProductInventoryResponse:
    inventory = purchased_inventory_service.adjust_purchased_product(
        db, business, product_id, payload
    )
    return _inventory_to_response(inventory)


@router.put(
    "/{product_id}/purchased-inventory/replacement-cost",
    dependencies=[Depends(require_csrf)],
)
def set_replacement_cost(
    product_id: uuid.UUID,
    payload: ReplacementCostRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> PurchasedProductInventoryResponse:
    inventory = purchased_inventory_service.set_replacement_cost(db, business, product_id, payload)
    return _inventory_to_response(inventory)


@router.get("/{product_id}/purchased-inventory/transactions")
def list_purchased_inventory_transactions(
    product_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[InventoryTransactionResponse]:
    items, total = purchased_inventory_service.list_purchased_inventory_transactions(
        db, business, product_id, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_transaction_to_response(t) for t in items], total=total, limit=limit, offset=offset
    )


# --- Top-level Inventory-module list (mounted at /api/v1/purchased-inventory) ----------


@list_router.get("")
def list_purchased_inventory(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[PurchasedProductInventorySummary]:
    items, total = purchased_inventory_service.list_purchased_inventory_for_business(
        db, business, is_active=is_active, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_summary_to_response(product, inventory) for product, inventory in items],
        total=total,
        limit=limit,
        offset=offset,
    )
