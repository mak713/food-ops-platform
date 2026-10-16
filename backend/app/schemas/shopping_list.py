"""Response schemas for the Shopping List report (Spec §12.12; Phase 9).

A *report* resource, not a collection of resources — ADR-101's `PageResponse` envelope
applies to resource lists, not to a bounded derived report whose ordering (shortage-first)
is part of its meaning. All quantities are canonical-unit Decimals computed by the
backend; the frontend only formats them (Spec §12.5).
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from pydantic import BaseModel


class ShoppingListDateShortageResponse(BaseModel):
    demand_date: date
    cumulative_reserved_through_date: Decimal
    cumulative_shortage_through_date: Decimal


class ShoppingListOrderResponse(BaseModel):
    order_id: str
    order_number: str
    status: str
    customer_name: str | None
    fulfillment_date: date | None
    fulfillment_time: time | None
    demand_quantity: Decimal


class ShoppingListContributorResponse(BaseModel):
    requirement_id: str
    product_id: str
    product_name: str
    recipe_revision_id: str | None
    demand_date: date
    reserved_quantity: Decimal
    is_overdue: bool
    is_in_production: bool
    orders: list[ShoppingListOrderResponse]


class ShoppingListItemResponse(BaseModel):
    ingredient_id: str
    ingredient_name: str
    canonical_unit: str
    physical_quantity: Decimal
    reserved_through_end: Decimal
    shortage_quantity: Decimal
    needed_by_date: date | None
    by_date: list[ShoppingListDateShortageResponse]
    contributors: list[ShoppingListContributorResponse]


class IncompleteRecipeNoticeResponse(BaseModel):
    requirement_id: str
    product_id: str
    product_name: str
    demand_date: date
    demand_quantity: Decimal


class ShoppingListRangeResponse(BaseModel):
    start_date: date
    end_date: date
    horizon_days: int | None
    is_custom: bool


class ShoppingListResponse(BaseModel):
    business_today: date
    range: ShoppingListRangeResponse
    shortages: list[ShoppingListItemResponse]
    covered: list[ShoppingListItemResponse]
    incomplete_recipe: list[IncompleteRecipeNoticeResponse]
