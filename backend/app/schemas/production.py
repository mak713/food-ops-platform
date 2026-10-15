"""Request/response schemas for the Production Run lifecycle (RUN-001..013; Phase 8
Final Architecture Lock §5/§6/§12)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class ProductionStartRequest(BaseModel):
    production_requirement_id: uuid.UUID
    batches: int | None = None
    acknowledged_warning_fingerprint: str | None = None


class ProductionFinishRequest(BaseModel):
    version: int
    actual_output_quantity: Decimal
    ingredient_actual_quantities: dict[uuid.UUID, Decimal] | None = None


class ProductionCancelRequest(BaseModel):
    version: int
    reason: str | None = None


class ProductionRunIngredientResponse(BaseModel):
    ingredient_id: str
    ingredient_name: str
    planned_quantity: Decimal
    actual_quantity: Decimal | None
    weighted_average_unit_cost_snapshot: Decimal
    replacement_unit_cost_snapshot: Decimal | None
    cost_basis_source: str
    actual_unit_cost_basis: Decimal | None
    actual_total_cost: Decimal | None


class ProductionRunResponse(BaseModel):
    id: str
    business_id: str
    source_production_requirement_id: str | None
    product_id: str
    product_name: str
    recipe_revision_id: str
    status: str
    planned_batches: int
    planned_output_quantity: Decimal
    actual_output_quantity: Decimal | None
    recipe_yield_snapshot: Decimal
    active_minutes_per_batch_snapshot: int
    elapsed_minutes_per_batch_snapshot: int | None
    labor_rate_snapshot: Decimal
    estimated_ingredient_cost: Decimal
    estimated_labor_cost: Decimal
    estimated_total_production_cost: Decimal
    actual_ingredient_cost: Decimal | None
    actual_labor_cost: Decimal | None
    actual_total_production_cost: Decimal | None
    started_at: datetime
    completed_at: datetime | None
    canceled_at: datetime | None
    version: int
    run_ingredients: list[ProductionRunIngredientResponse] = []


class ProductionFinishResponse(BaseModel):
    run: ProductionRunResponse
    negative_inventory_ingredient_ids: list[str]
    is_shortfall: bool


class ProductionRequirementResponse(BaseModel):
    id: str
    product_id: str
    product_name: str
    recipe_revision_id: str | None
    demand_date: date
    calculation_status: str
    confirmed_demand_quantity: Decimal
    surplus_allocated_quantity: Decimal
    production_demand_quantity: Decimal
    recommended_batches: int | None
    expected_output_quantity: Decimal | None
    expected_excess_quantity: Decimal | None
    estimated_active_minutes: int | None
    estimated_elapsed_minutes: int | None
    suggested_start_at: datetime | None
    estimated_ingredient_cost: Decimal | None
    estimated_labor_cost: Decimal | None
    estimated_direct_production_cost: Decimal | None
