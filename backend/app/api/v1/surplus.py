"""Surplus endpoints: manual creation, reduction-only adjustment, list, and history
(SUR-008; Phase 8 Final Architecture Lock §9, corrected by Final Architecture
Amendment §3 and Final Architecture Lock Addendum §B). Thin router per
`CLAUDE.md`/Spec §11.2 — validation and business logic live in
`app.services.surplus_service`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.core.tenant import get_owned_or_404
from app.db.models.business import Business
from app.db.models.surplus import SurplusInventory, SurplusTransaction
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.surplus import SurplusAdjustmentRequest, SurplusManualCreateRequest
from app.services import surplus_service

router = APIRouter()


def _business_today(business: Business) -> date:
    return datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()


def _to_response(lot: SurplusInventory) -> dict:
    return {
        "id": str(lot.id),
        "product_id": str(lot.product_id),
        "product_name": lot.product.name,
        "source_production_run_id": (
            str(lot.source_production_run_id) if lot.source_production_run_id else None
        ),
        "physical_quantity": lot.physical_quantity,
        "unit_cost_basis": lot.unit_cost_basis,
        "source_total_cost_basis": lot.source_total_cost_basis,
        "produced_at": lot.produced_at,
        "usable_through_date": lot.usable_through_date,
        "is_reusable": lot.is_reusable,
        "notes": lot.notes,
        "version": lot.version,
    }


def _transaction_to_response(txn: SurplusTransaction) -> dict:
    return {
        "id": str(txn.id),
        "surplus_inventory_id": str(txn.surplus_inventory_id),
        "transaction_type": txn.transaction_type.value,
        "quantity_change": txn.quantity_change,
        "reason": txn.reason,
        "notes": txn.notes,
        "order_id": str(txn.order_id) if txn.order_id else None,
        "production_run_id": str(txn.production_run_id) if txn.production_run_id else None,
        "created_at": txn.created_at,
    }


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_surplus(
    payload: SurplusManualCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    lot = surplus_service.create_surplus_manual(
        db, business, payload, business_today=_business_today(business)
    )
    return _to_response(lot)


@router.get("")
def list_surplus(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[dict]:
    items, total = surplus_service.list_surplus_for_business(
        db, business, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_to_response(i) for i in items], total=total, limit=limit, offset=offset
    )


@router.get("/{surplus_id}")
def get_surplus(
    surplus_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    lot = get_owned_or_404(db, SurplusInventory, surplus_id, business)
    return _to_response(lot)


@router.post("/{surplus_id}/adjust", dependencies=[Depends(require_csrf)])
def adjust_surplus(
    surplus_id: uuid.UUID,
    payload: SurplusAdjustmentRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    lot = surplus_service.adjust_surplus(
        db, business, surplus_id, payload, business_today=_business_today(business)
    )
    return _to_response(lot)


@router.get("/{surplus_id}/transactions")
def list_surplus_transactions(
    surplus_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[dict]:
    items, total = surplus_service.list_surplus_transactions(
        db, business, surplus_id, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_transaction_to_response(t) for t in items], total=total, limit=limit, offset=offset
    )
