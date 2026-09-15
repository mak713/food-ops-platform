"""Order/OrderLine/Payment endpoints (Phase 6 Final Plan §H).

Thin router — business logic lives in `app.services.order_service`/`payment_service`,
pure arithmetic in `app.domain.order_pricing`. Every mutating route depends on
`require_csrf` individually (GET routes must not require it). No `POST .../confirm`,
Cancel, Ready, or Complete route exists — Phase 6 never persists `DRAFT -> CONFIRMED`
(Final Plan §C)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.db.enums import OrderStatus
from app.db.models.business import Business
from app.db.models.order import Order, OrderLine, Payment
from app.db.session import get_db
from app.domain import order_pricing
from app.schemas.common import PageResponse
from app.schemas.order import (
    ConfirmationIssueResponse,
    OrderCreateRequest,
    OrderLineResponse,
    OrderResponse,
    OrderSummary,
    OrderUpdateRequest,
    PaymentCreateRequest,
    PaymentResponse,
)
from app.services import order_service, payment_service

router = APIRouter()


def _line_to_response(line: OrderLine) -> OrderLineResponse:
    return OrderLineResponse(
        id=str(line.id),
        line_type=line.line_type,
        product_id=str(line.product_id) if line.product_id else None,
        selling_option_id=str(line.selling_option_id) if line.selling_option_id else None,
        display_name_snapshot=line.display_name_snapshot,
        package_quantity=line.package_quantity,
        underlying_quantity=line.underlying_quantity,
        charged_unit_price_snapshot=line.charged_unit_price_snapshot,
        line_subtotal=line.line_subtotal,
        packaging_cost_per_package_snapshot=line.packaging_cost_per_package_snapshot,
        packaging_cost_total_snapshot=line.packaging_cost_total_snapshot,
        price_override_reason=line.price_override_reason,
        custom_direct_cost_estimate=line.custom_direct_cost_estimate,
        custom_active_time_minutes=line.custom_active_time_minutes,
        manual_fulfillment_required=line.manual_fulfillment_required,
        manual_fulfillment_satisfied=line.manual_fulfillment_satisfied,
        notes=line.notes,
    )


def _payment_to_response(payment: Payment) -> PaymentResponse:
    return PaymentResponse(
        id=str(payment.id),
        amount=payment.amount,
        payment_method=payment.payment_method,
        payment_date=payment.payment_date,
        notes=payment.notes,
    )


def _to_response(order: Order) -> OrderResponse:
    payments_total = order_pricing.quantize_money(
        sum((p.amount for p in order.payments), Decimal("0"))
    )
    payment_status = order_pricing.derive_payment_status(payments_total, order.final_total)
    is_overpaid = payments_total > order.final_total
    overpayment_amount = payments_total - order.final_total if is_overpaid else None

    issues = order_pricing.check_structural_confirmation_readiness(
        status=order.status.value,
        line_count=len(order.lines),
        fulfillment_date_present=order.fulfillment_date is not None,
    )

    return OrderResponse(
        id=str(order.id),
        order_number=order.order_number,
        status=order.status,
        customer_id=str(order.customer_id) if order.customer_id else None,
        fulfillment_date=order.fulfillment_date,
        fulfillment_time=order.fulfillment_time,
        fulfillment_method=order.fulfillment_method,
        fulfillment_details=order.fulfillment_details,
        fulfillment_notes=order.fulfillment_notes,
        internal_notes=order.internal_notes,
        subtotal=order.subtotal,
        order_adjustment=order.order_adjustment,
        adjustment_description=order.adjustment_description,
        manual_tax=order.manual_tax,
        final_total=order.final_total,
        estimated_direct_cost=order.estimated_direct_cost,
        estimated_contribution=order.estimated_contribution,
        estimated_contribution_margin=order.estimated_contribution_margin,
        version=order.version,
        lines=[_line_to_response(line) for line in order.lines],
        payments=[_payment_to_response(payment) for payment in order.payments],
        payments_total=payments_total,
        payment_status=payment_status.value,
        overpayment_amount=overpayment_amount,
        is_confirmable=not issues,
        confirmation_issues=[
            ConfirmationIssueResponse(
                severity=issue.severity, code=issue.code, message=issue.message, field=issue.field
            )
            for issue in issues
        ],
    )


def _to_summary(order: Order) -> OrderSummary:
    payments_total = order_pricing.quantize_money(
        sum((p.amount for p in order.payments), Decimal("0"))
    )
    payment_status = order_pricing.derive_payment_status(payments_total, order.final_total)
    return OrderSummary(
        id=str(order.id),
        order_number=order.order_number,
        status=order.status,
        customer_id=str(order.customer_id) if order.customer_id else None,
        customer_name=order.customer.name if order.customer else None,
        fulfillment_date=order.fulfillment_date,
        final_total=order.final_total,
        payment_status=payment_status.value,
        version=order.version,
    )


@router.get("")
def list_orders(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    q: str | None = None,
    status: OrderStatus | None = None,
    customer_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[OrderSummary]:
    items, total = order_service.list_orders_for_business(
        db, business, q=q, status=status, customer_id=customer_id, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_to_summary(o) for o in items], total=total, limit=limit, offset=offset
    )


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_order(
    payload: OrderCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order = order_service.create_draft_order(db, business, payload)
    return _to_response(order)


@router.get("/{order_id}")
def get_order(
    order_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order = order_service.get_order_for_business(db, order_id, business)
    return _to_response(order)


@router.patch("/{order_id}", dependencies=[Depends(require_csrf)])
def update_order(
    order_id: uuid.UUID,
    payload: OrderUpdateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order = order_service.update_draft_order(db, business, order_id, payload)
    return _to_response(order)


@router.delete("/{order_id}", status_code=204, dependencies=[Depends(require_csrf)])
def delete_order(
    order_id: uuid.UUID,
    version: int,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    confirm_delete_with_payments: bool = False,
) -> None:
    order_service.delete_draft_order(
        db,
        business,
        order_id,
        version,
        confirm_delete_with_payments=confirm_delete_with_payments,
    )


@router.post("/{order_id}/payments", status_code=201, dependencies=[Depends(require_csrf)])
def add_payment(
    order_id: uuid.UUID,
    payload: PaymentCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order = payment_service.add_payment(db, business, order_id, payload)
    return _to_response(order)
