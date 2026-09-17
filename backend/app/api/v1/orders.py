"""Order/OrderLine/Payment endpoints (Phase 6 Final Plan §H; Phase 7 Plan v2 §15).

Thin router — business logic lives in `app.services.order_service`/`payment_service`/
`order_lifecycle_service`, pure arithmetic in `app.domain.order_pricing`. Every
mutating route depends on `require_csrf` individually (GET routes must not require
it). Phase 7 adds real `POST .../confirm` and `POST .../cancel` — Phase 6's own
`DRAFT`-only persistence and structural-readiness-only confirmation are otherwise
unchanged."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

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
    CustomItemWorkloadPreviewItem,
    IngredientAvailabilityPreviewItem,
    OperationalPreviewResponse,
    OperationalWarningResponse,
    OrderCancelRequest,
    OrderConfirmedEditRequest,
    OrderConfirmRequest,
    OrderCreateRequest,
    OrderLineResponse,
    OrderResponse,
    OrderSummary,
    PaymentCreateRequest,
    PaymentResponse,
    ProductionRequirementPreviewItem,
    PurchasedShortagePreviewItem,
)
from app.services import (
    order_lifecycle_service,
    order_preview_service,
    order_service,
    payment_service,
)

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


def _to_response(order: Order, *, db: Session, business: Business) -> OrderResponse:
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
    production_locked = bool(
        order_lifecycle_service.get_order_production_locked_line_ids(db, business, order)
        if order.status is OrderStatus.CONFIRMED
        else False
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
        production_locked=production_locked,
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
    return _to_response(order, db=db, business=business)


@router.get("/{order_id}")
def get_order(
    order_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order = order_service.get_order_for_business(db, order_id, business)
    return _to_response(order, db=db, business=business)


@router.patch("/{order_id}", dependencies=[Depends(require_csrf)])
def update_order(
    order_id: uuid.UUID,
    payload: OrderConfirmedEditRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    """DRAFT edits are unchanged Phase 6 behavior. A CONFIRMED order is dispatched
    to the Phase 7 `update_confirmed_order` workflow instead (Implementation
    Remediation Plan, Finding 1) — `payload` is a superset of `OrderUpdateRequest`
    (adds `acknowledged_warning_fingerprints`) so either service function accepts it
    unchanged. The status peek below is routing only; each service function
    independently re-checks status under its own row lock, so a concurrent status
    change between the peek and the lock is always caught by the callee itself,
    never a correctness gap here."""
    peek = order_service.get_order_for_business(db, order_id, business)
    if peek.status is OrderStatus.CONFIRMED:
        order, _results = order_lifecycle_service.update_confirmed_order(
            db,
            business,
            order_id,
            payload,
            expected_version=payload.version,
            acknowledged_warning_fingerprints=set(payload.acknowledged_warning_fingerprints),
            business_today=_business_today(business),
        )
    else:
        order = order_service.update_draft_order(db, business, order_id, payload)
    return _to_response(order, db=db, business=business)


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
    return _to_response(order, db=db, business=business)


def _business_today(business: Business) -> date:
    """Business-local "today" (Final Pre-Implementation Amendment §2): the single
    derivation point for every "today" comparison Phase 7 recalculation performs."""
    return datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()


def _preview_result_to_response(result) -> OperationalPreviewResponse:
    return OperationalPreviewResponse(
        subtotal=result.subtotal,
        final_total=result.final_total,
        warnings=[
            OperationalWarningResponse(
                severity=issue["severity"],
                code=issue["code"],
                message=issue["message"],
                field=issue["field"],
                resource=issue["resource"],
                details=issue["details"],
            )
            for issue in result.warning_issues
        ],
        warning_fingerprints=sorted(result.warning_fingerprints),
        production_requirements=[
            ProductionRequirementPreviewItem(
                product_id=str(item.product_id),
                recipe_revision_id=(
                    str(item.recipe_revision_id) if item.recipe_revision_id else None
                ),
                demand_date=item.demand_date,
                is_protected=item.is_protected,
                missing_recipe=item.missing_recipe,
                baseline_confirmed_demand_quantity=item.baseline.confirmed_demand_quantity,
                baseline_surplus_allocated_quantity=item.baseline.surplus_allocated_quantity,
                baseline_production_demand_quantity=item.baseline.production_demand_quantity,
                baseline_recommended_batches=item.baseline.recommended_batches,
                projected_confirmed_demand_quantity=item.projected.confirmed_demand_quantity,
                projected_surplus_allocated_quantity=item.projected.surplus_allocated_quantity,
                projected_production_demand_quantity=item.projected.production_demand_quantity,
                projected_recommended_batches=item.projected.recommended_batches,
                projected_expected_output_quantity=item.projected.expected_output_quantity,
                projected_expected_excess_quantity=item.projected.expected_excess_quantity,
                projected_estimated_active_minutes=item.projected.estimated_active_minutes,
                projected_estimated_elapsed_minutes=item.projected.estimated_elapsed_minutes,
                projected_estimated_ingredient_cost=item.projected.estimated_ingredient_cost,
                projected_estimated_labor_cost=item.projected.estimated_labor_cost,
                projected_estimated_direct_production_cost=(
                    item.projected.estimated_direct_production_cost
                ),
                projected_suggested_start_at=item.projected.suggested_start_at,
                incremental_confirmed_demand_quantity=(
                    item.projected.confirmed_demand_quantity
                    - item.baseline.confirmed_demand_quantity
                ),
                incremental_production_demand_quantity=(
                    item.projected.production_demand_quantity
                    - item.baseline.production_demand_quantity
                ),
            )
            for item in result.requirement_previews
        ],
        ingredient_availability=[
            IngredientAvailabilityPreviewItem(
                ingredient_id=str(item.ingredient_id),
                ingredient_name=item.ingredient_name,
                canonical_unit=item.canonical_unit,
                physical_quantity=item.physical_quantity,
                baseline_shortage_quantity=item.baseline_shortage_quantity,
                projected_shortage_quantity=item.projected_shortage_quantity,
            )
            for item in result.ingredient_availability
        ],
        purchased_shortages=[
            PurchasedShortagePreviewItem(
                product_id=str(item.product_id),
                baseline_shortage_quantity=item.baseline_shortage_quantity,
                projected_shortage_quantity=item.projected_shortage_quantity,
            )
            for item in result.purchased_shortages
        ],
        custom_item_workload=[
            CustomItemWorkloadPreviewItem(
                demand_date=item.demand_date,
                baseline_total_active_minutes=item.baseline_total_active_minutes,
                projected_total_active_minutes=item.projected_total_active_minutes,
                baseline_contributing_line_count=len(item.baseline_contributing_order_line_ids),
                projected_contributing_line_count=len(item.projected_contributing_order_line_ids),
            )
            for item in result.custom_item_workload
        ],
        fulfillment_date_required_for_operational_preview=(
            result.fulfillment_date_required_for_operational_preview
        ),
    )


@router.post("/preview")
def preview_order(
    payload: OrderCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OperationalPreviewResponse:
    """Draft Operational Preview for an unsaved new Order (Spec §11.5; Plan v2 §12).
    Zero writes — see `order_preview_service.preview_order`'s own docstring for the
    exact scope of what this endpoint covers."""
    result = order_preview_service.preview_order(
        db, business, payload, business_today=_business_today(business)
    )
    return _preview_result_to_response(result)


@router.post("/{order_id}/preview")
def preview_existing_order(
    order_id: uuid.UUID,
    payload: OrderCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OperationalPreviewResponse:
    """Draft Operational Preview for a persisted DRAFT or CONFIRMED Order's proposed
    edit (Phase 7 Implementation Remediation Plan, Finding 2). Zero writes. For a
    CONFIRMED Order, uses the Amendment §4 substitution composition — see
    `order_preview_service.preview_order`'s own docstring."""
    result = order_preview_service.preview_order(
        db, business, payload, order_id=order_id, business_today=_business_today(business)
    )
    return _preview_result_to_response(result)


@router.post("/{order_id}/confirm", dependencies=[Depends(require_csrf)])
def confirm_order(
    order_id: uuid.UUID,
    payload: OrderConfirmRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order, _results = order_lifecycle_service.confirm_order(
        db,
        business,
        order_id,
        expected_version=payload.version,
        acknowledged_warning_fingerprints=set(payload.acknowledged_warning_fingerprints),
        business_today=_business_today(business),
    )
    return _to_response(order, db=db, business=business)


@router.post("/{order_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_order(
    order_id: uuid.UUID,
    payload: OrderCancelRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    order, _results = order_lifecycle_service.cancel_confirmed_order(
        db,
        business,
        order_id,
        expected_version=payload.version,
        business_today=_business_today(business),
    )
    return _to_response(order, db=db, business=business)
