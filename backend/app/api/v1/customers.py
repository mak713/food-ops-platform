"""Customer CRUD/list/archive/reactivate/delete endpoints (Phase 3 plan v3 §12).

Thin routers per `CLAUDE.md`/Spec §11.2 — validation and business logic live in
`app.services.customer_service`. Every mutating route depends on `require_csrf`
individually (never router-wide), matching the existing `account.py` pattern, since GET
routes must not require CSRF.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.customer import (
    CustomerCreateRequest,
    CustomerResponse,
    CustomerSummary,
    CustomerUpdateRequest,
    LifecycleActionRequest,
)
from app.services import customer_service

router = APIRouter()


def _to_response(customer: Customer) -> CustomerResponse:
    return CustomerResponse(
        id=str(customer.id),
        name=customer.name,
        phone=customer.phone,
        email=customer.email,
        preferred_contact_method=customer.preferred_contact_method,
        notes=customer.notes,
        is_active=customer.is_active,
        version=customer.version,
    )


def _to_summary(customer: Customer) -> CustomerSummary:
    return CustomerSummary(
        id=str(customer.id),
        name=customer.name,
        phone=customer.phone,
        email=customer.email,
        is_active=customer.is_active,
        version=customer.version,
    )


@router.get("")
def list_customers(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    q: str | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[CustomerSummary]:
    items, total = customer_service.list_customers_for_business(
        db, business, q=q, is_active=is_active, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_to_summary(c) for c in items], total=total, limit=limit, offset=offset
    )


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_customer(
    payload: CustomerCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> CustomerResponse:
    customer = customer_service.create_customer(db, business, payload)
    return _to_response(customer)


@router.get("/{customer_id}")
def get_customer(
    customer_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> CustomerResponse:
    customer = customer_service.get_customer_for_business(db, customer_id, business)
    return _to_response(customer)


@router.patch("/{customer_id}", dependencies=[Depends(require_csrf)])
def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> CustomerResponse:
    customer = customer_service.update_customer(db, business, customer_id, payload)
    return _to_response(customer)


@router.post("/{customer_id}/archive", dependencies=[Depends(require_csrf)])
def archive_customer(
    customer_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> CustomerResponse:
    customer = customer_service.archive_customer(db, business, customer_id, payload.version)
    return _to_response(customer)


@router.post("/{customer_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_customer(
    customer_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> CustomerResponse:
    customer = customer_service.reactivate_customer(db, business, customer_id, payload.version)
    return _to_response(customer)


@router.delete("/{customer_id}", status_code=204, dependencies=[Depends(require_csrf)])
def delete_customer(
    customer_id: uuid.UUID,
    version: int,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    customer_service.delete_customer(db, business, customer_id, version)
