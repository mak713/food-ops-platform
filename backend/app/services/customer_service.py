"""Customer CRUD, archive/reactivate/safe-delete, and duplicate-warning business logic
(Phase 3 plan v3 §5/§7/§11).

Follows the Phase 2 convention (see `app/services/auth_service.py`): plain module-level
functions, each owning its own transaction boundary via an explicit `db.commit()`.
"""

from __future__ import annotations

import uuid

from psycopg.errors import ForeignKeyViolation
from sqlalchemy import func, or_, select
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
from app.db.models.customer import Customer
from app.schemas.customer import CustomerCreateRequest, CustomerUpdateRequest


def get_customer_for_business(db: Session, customer_id: uuid.UUID, business: Business) -> Customer:
    return get_owned_or_404(db, Customer, customer_id, business)


def _find_duplicate(
    db: Session, business: Business, *, name: str, phone: str | None, email: str | None
) -> Customer | None:
    """Advisory duplicate match (Spec §17.6; Phase 3 plan v3 §11): case-insensitive exact
    match on name, OR exact match on (already-normalized) phone, OR exact match on
    (already-normalized) email. No fuzzy matching."""
    conditions = [func.lower(Customer.name) == name.lower()]
    if phone:
        conditions.append(Customer.phone == phone)
    if email:
        conditions.append(func.lower(Customer.email) == email.lower())
    return db.scalar(
        select(Customer).where(Customer.business_id == business.id, or_(*conditions)).limit(1)
    )


def create_customer(db: Session, business: Business, payload: CustomerCreateRequest) -> Customer:
    if not payload.confirm_duplicate:
        duplicate = _find_duplicate(
            db, business, name=payload.name, phone=payload.phone, email=payload.email
        )
        if duplicate is not None:
            raise ApiError(
                422,
                "CUSTOMER_CREATE_WARNING",
                "A customer that looks like a duplicate already exists.",
                issues=[
                    {
                        "severity": "WARNING",
                        "code": "POSSIBLE_DUPLICATE_CUSTOMER",
                        "message": (
                            "A customer with a matching name, phone, or email already exists."
                        ),
                        "field": "name",
                        "resource": "customer",
                        "details": {"matched_customer_id": str(duplicate.id)},
                    }
                ],
            )

    customer = Customer(
        id=uuid.uuid4(),
        business_id=business.id,
        name=payload.name,
        phone=payload.phone,
        email=payload.email,
        preferred_contact_method=payload.preferred_contact_method,
        notes=payload.notes,
    )
    db.add(customer)
    db.commit()
    return customer


def list_customers_for_business(
    db: Session,
    business: Business,
    *,
    q: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Customer], int]:
    conditions = [Customer.business_id == business.id]
    if q:
        conditions.append(Customer.name.ilike(f"%{q}%"))
    if is_active is not None:
        conditions.append(Customer.is_active == is_active)

    total = db.scalar(select(func.count()).select_from(Customer).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Customer)
            .where(*conditions)
            .order_by(Customer.name.asc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def update_customer(
    db: Session, business: Business, customer_id: uuid.UUID, payload: CustomerUpdateRequest
) -> Customer:
    customer = get_customer_for_business(db, customer_id, business)
    check_version(customer, payload.version, resource="customer")

    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    for field, value in changes.items():
        setattr(customer, field, value)
    if changes:
        commit_or_raise_stale(db, resource="customer")
    return customer


def archive_customer(
    db: Session, business: Business, customer_id: uuid.UUID, expected_version: int
) -> Customer:
    customer = get_customer_for_business(db, customer_id, business)
    check_version(customer, expected_version, resource="customer")
    if customer.is_active:
        customer.is_active = False
        commit_or_raise_stale(db, resource="customer")
    return customer


def reactivate_customer(
    db: Session, business: Business, customer_id: uuid.UUID, expected_version: int
) -> Customer:
    customer = get_customer_for_business(db, customer_id, business)
    check_version(customer, expected_version, resource="customer")
    if not customer.is_active:
        customer.is_active = True
        commit_or_raise_stale(db, resource="customer")
    return customer


def delete_customer(
    db: Session, business: Business, customer_id: uuid.UUID, expected_version: int
) -> None:
    """Safe-delete (Spec §8.32; Phase 3 plan v3 §5): attempt the delete and translate only
    a genuine foreign-key violation (SQLSTATE 23503, `orders.customer_id` is the only
    reference, `ondelete="NO ACTION"`) into a 409. Any other IntegrityError is not our
    case to handle here and is re-raised for the existing generic error handler."""
    customer = get_customer_for_business(db, customer_id, business)
    check_version(customer, expected_version, resource="customer")

    try:
        db.delete(customer)
        db.commit()
    except StaleDataError:
        # Distinct from the FK-reference case below (Phase 3 plan v3 §18): the DELETE's
        # own WHERE id=? AND version=? matched zero rows because someone else mutated the
        # row between our check_version() pre-check and this commit.
        db.rollback()
        raise stale_version_error(resource="customer") from None
    except IntegrityError as exc:
        db.rollback()
        if isinstance(exc.orig, ForeignKeyViolation):
            raise ApiError(
                409,
                "CUSTOMER_HAS_ORDER_HISTORY",
                "This customer has order history and cannot be deleted. Archive it instead.",
            ) from None
        raise
