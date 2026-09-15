"""Payment recording (Phase 6 Final Plan §G).

Payments are internally recorded positive events — no processing provider, no refunds,
no edit/delete of an individual Payment (append-only in V1, the sole exception being the
existing Draft-deletion cascade in `order_service.delete_draft_order`). Adding a Payment
never mutates the Order aggregate's own state, so it never calls `order_service.touch_order`
— it is a separate historical-child operation, governed by the Order row lock alone.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderStatus
from app.db.models.business import Business
from app.db.models.order import Order, Payment
from app.schemas.order import PaymentCreateRequest
from app.services import order_service


def add_payment(
    db: Session, business: Business, order_id: uuid.UUID, payload: PaymentCreateRequest
) -> Order:
    """Exact sequence (Final Plan §G; Final Pre-Implementation Amendment §7):
    1. tenant-scope and lock the parent Order row (the shared serialization point with
       `order_service.update_draft_order`);
    2. reject a Payment against a Canceled order;
    3. query a FRESH `SUM(payments.amount)` directly from the database, issued only after
       the lock is held — never a possibly-stale, possibly-pre-loaded `order.payments`
       relationship collection;
    4. evaluate the overpayment warning against that fresh sum;
    5. if unacknowledged overpayment: roll back, raise the warning, insert nothing;
    6. otherwise insert the Payment and commit.

    No `order_version` field is requested from the client for this endpoint — inserting a
    Payment never updates `Order.version_id_col`, so a version check here would guard
    nothing a real `StaleDataError` could ever raise; the row lock is the sole, sufficient
    concurrency mechanism."""
    order = order_service.get_order_for_business_locked(db, order_id, business)

    if order.status is OrderStatus.CANCELED:
        db.rollback()
        raise ApiError(
            409,
            "ORDER_CANCELED_NO_NEW_PAYMENTS",
            "Payments cannot be recorded against a canceled order.",
        )

    payments_total = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.order_id == order.id)
    )
    new_total = payments_total + payload.amount
    if new_total > order.final_total and not payload.confirm_overpayment:
        db.rollback()
        raise order_service.payment_overage_error(
            existing_total=payments_total,
            final_total=order.final_total,
            proposed=payload.amount,
        )

    payment = Payment(
        id=uuid.uuid4(),
        business_id=business.id,
        order_id=order.id,
        amount=payload.amount,
        payment_method=payload.payment_method,
        payment_date=payload.payment_date,
        notes=payload.notes,
    )
    db.add(payment)
    db.commit()
    return order
