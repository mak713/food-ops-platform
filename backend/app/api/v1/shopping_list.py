"""Shopping List endpoint (Spec §12.12, PLAN-011; Phase 9). Thin router per
`CLAUDE.md`/Spec §11.2 — range validation and derivation live in
`app.services.shopping_list_service`. Read-only: no CSRF requirement, no locks, no
writes. The Business is derived solely from the authenticated session (SEC-002); no
`business_id` parameter exists."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_business
from app.db.models.business import Business
from app.db.session import get_db
from app.schemas.shopping_list import (
    IncompleteRecipeNoticeResponse,
    ShoppingListContributorResponse,
    ShoppingListDateShortageResponse,
    ShoppingListItemResponse,
    ShoppingListOrderResponse,
    ShoppingListRangeResponse,
    ShoppingListResponse,
)
from app.services import shopping_list_service
from app.services.shopping_list_service import ShoppingListItem

router = APIRouter()


def _business_today(business: Business) -> date:
    return datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()


def _item_to_response(item: ShoppingListItem) -> ShoppingListItemResponse:
    return ShoppingListItemResponse(
        ingredient_id=str(item.ingredient_id),
        ingredient_name=item.ingredient_name,
        canonical_unit=item.canonical_unit,
        physical_quantity=item.physical_quantity,
        reserved_through_end=item.reserved_through_end,
        shortage_quantity=item.shortage_quantity,
        needed_by_date=item.needed_by_date,
        by_date=[
            ShoppingListDateShortageResponse(
                demand_date=entry.demand_date,
                cumulative_reserved_through_date=entry.cumulative_reserved_through_date,
                cumulative_shortage_through_date=entry.cumulative_shortage_through_date,
            )
            for entry in item.by_date
        ],
        contributors=[
            ShoppingListContributorResponse(
                requirement_id=str(c.requirement_id),
                product_id=str(c.product_id),
                product_name=c.product_name,
                recipe_revision_id=str(c.recipe_revision_id) if c.recipe_revision_id else None,
                demand_date=c.demand_date,
                reserved_quantity=c.reserved_quantity,
                is_overdue=c.is_overdue,
                is_in_production=c.is_in_production,
                orders=[
                    ShoppingListOrderResponse(
                        order_id=str(o.order_id),
                        order_number=o.order_number,
                        status=o.status,
                        customer_name=o.customer_name,
                        fulfillment_date=o.fulfillment_date,
                        fulfillment_time=o.fulfillment_time,
                        demand_quantity=o.demand_quantity,
                    )
                    for o in c.orders
                ],
            )
            for c in item.contributors
        ],
    )


@router.get("")
def get_shopping_list(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    horizon_days: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> ShoppingListResponse:
    today = _business_today(business)
    report = shopping_list_service.get_shopping_list(
        db,
        business,
        business_today=today,
        horizon_days=horizon_days,
        start_date=start_date,
        end_date=end_date,
    )
    return ShoppingListResponse(
        business_today=today,
        range=ShoppingListRangeResponse(
            start_date=report.range.start_date,
            end_date=report.range.end_date,
            horizon_days=report.range.horizon_days,
            is_custom=report.range.is_custom,
        ),
        shortages=[_item_to_response(i) for i in report.shortages],
        covered=[_item_to_response(i) for i in report.covered],
        incomplete_recipe=[
            IncompleteRecipeNoticeResponse(
                requirement_id=str(n.requirement_id),
                product_id=str(n.product_id),
                product_name=n.product_name,
                demand_date=n.demand_date,
                demand_quantity=n.demand_quantity,
            )
            for n in report.incomplete_recipe
        ],
    )
