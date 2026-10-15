"""Production Run lifecycle endpoints (RUN-001..013; Phase 8 Final Architecture Lock
§5/§6/§12). Thin router per `CLAUDE.md`/Spec §11.2 — validation and business logic
live in `app.services.production_run_service`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.core.tenant import get_owned_or_404
from app.db.enums import ProductionRunStatus
from app.db.models.business import Business
from app.db.models.production import ProductionRequirement, ProductionRun, ProductionRunIngredient
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.production import (
    ProductionCancelRequest,
    ProductionFinishRequest,
    ProductionFinishResponse,
    ProductionRequirementResponse,
    ProductionRunIngredientResponse,
    ProductionRunResponse,
    ProductionStartRequest,
)
from app.services import production_run_service

router = APIRouter()


def _business_today(business: Business) -> date:
    """Business-local "today" — the same derivation `orders.py` already uses."""
    return datetime.now(UTC).astimezone(ZoneInfo(business.timezone)).date()


def _run_ingredient_to_response(ri: ProductionRunIngredient) -> ProductionRunIngredientResponse:
    return ProductionRunIngredientResponse(
        ingredient_id=str(ri.ingredient_id),
        ingredient_name=ri.ingredient.name,
        planned_quantity=ri.planned_quantity,
        actual_quantity=ri.actual_quantity,
        weighted_average_unit_cost_snapshot=ri.weighted_average_unit_cost_snapshot,
        replacement_unit_cost_snapshot=ri.replacement_unit_cost_snapshot,
        cost_basis_source=ri.cost_basis_source.value,
        actual_unit_cost_basis=ri.actual_unit_cost_basis,
        actual_total_cost=ri.actual_total_cost,
    )


def _run_to_response(
    run: ProductionRun, *, db: Session, business: Business
) -> ProductionRunResponse:
    run_ingredients = db.scalars(
        select(ProductionRunIngredient).where(
            ProductionRunIngredient.production_run_id == run.id,
            ProductionRunIngredient.business_id == business.id,
        )
    ).all()
    return ProductionRunResponse(
        id=str(run.id),
        business_id=str(run.business_id),
        source_production_requirement_id=(
            str(run.source_production_requirement_id)
            if run.source_production_requirement_id
            else None
        ),
        product_id=str(run.product_id),
        product_name=run.product.name,
        recipe_revision_id=str(run.recipe_revision_id),
        status=run.status.value,
        planned_batches=run.planned_batches,
        planned_output_quantity=run.planned_output_quantity,
        actual_output_quantity=run.actual_output_quantity,
        recipe_yield_snapshot=run.recipe_yield_snapshot,
        active_minutes_per_batch_snapshot=run.active_minutes_per_batch_snapshot,
        elapsed_minutes_per_batch_snapshot=run.elapsed_minutes_per_batch_snapshot,
        labor_rate_snapshot=run.labor_rate_snapshot,
        estimated_ingredient_cost=run.estimated_ingredient_cost,
        estimated_labor_cost=run.estimated_labor_cost,
        estimated_total_production_cost=run.estimated_total_production_cost,
        actual_ingredient_cost=run.actual_ingredient_cost,
        actual_labor_cost=run.actual_labor_cost,
        actual_total_production_cost=run.actual_total_production_cost,
        started_at=run.started_at,
        completed_at=run.completed_at,
        canceled_at=run.canceled_at,
        version=run.version,
        run_ingredients=[_run_ingredient_to_response(ri) for ri in run_ingredients],
    )


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def start_production(
    payload: ProductionStartRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductionRunResponse:
    run = production_run_service.start_production(
        db,
        business,
        payload.production_requirement_id,
        batches=payload.batches,
        acknowledged_warning_fingerprint=payload.acknowledged_warning_fingerprint,
    )
    return _run_to_response(run, db=db, business=business)


@router.get("")
def list_production_runs(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    status: ProductionRunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[ProductionRunResponse]:
    conditions = [ProductionRun.business_id == business.id]
    if status is not None:
        conditions.append(ProductionRun.status == status)
    total = db.scalar(select(func.count()).select_from(ProductionRun).where(*conditions)) or 0
    rows = db.scalars(
        select(ProductionRun)
        .where(*conditions)
        .order_by(ProductionRun.started_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return PageResponse(
        items=[_run_to_response(r, db=db, business=business) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _requirement_to_response(req: ProductionRequirement) -> ProductionRequirementResponse:
    return ProductionRequirementResponse(
        id=str(req.id),
        product_id=str(req.product_id),
        product_name=req.product.name,
        recipe_revision_id=str(req.recipe_revision_id) if req.recipe_revision_id else None,
        demand_date=req.demand_date,
        calculation_status=req.calculation_status.value,
        confirmed_demand_quantity=req.confirmed_demand_quantity,
        surplus_allocated_quantity=req.surplus_allocated_quantity,
        production_demand_quantity=req.production_demand_quantity,
        recommended_batches=req.recommended_batches,
        expected_output_quantity=req.expected_output_quantity,
        expected_excess_quantity=req.expected_excess_quantity,
        estimated_active_minutes=req.estimated_active_minutes,
        estimated_elapsed_minutes=req.estimated_elapsed_minutes,
        suggested_start_at=req.suggested_start_at,
        estimated_ingredient_cost=req.estimated_ingredient_cost,
        estimated_labor_cost=req.estimated_labor_cost,
        estimated_direct_production_cost=req.estimated_direct_production_cost,
    )


@router.get("/requirements")
def list_production_requirements(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[ProductionRequirementResponse]:
    """Production Plan/Review candidate list — every currently-actionable,
    not-already-active requirement, earliest demand date first."""
    items, total = production_run_service.list_production_requirements_for_business(
        db, business, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_requirement_to_response(r) for r in items], total=total, limit=limit, offset=offset
    )


@router.get("/requirements/{requirement_id}")
def get_production_requirement(
    requirement_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductionRequirementResponse:
    requirement = get_owned_or_404(db, ProductionRequirement, requirement_id, business)
    return _requirement_to_response(requirement)


@router.get("/{production_run_id}")
def get_production_run(
    production_run_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductionRunResponse:
    run = get_owned_or_404(db, ProductionRun, production_run_id, business)
    return _run_to_response(run, db=db, business=business)


@router.post("/{production_run_id}/finish", dependencies=[Depends(require_csrf)])
def finish_production(
    production_run_id: uuid.UUID,
    payload: ProductionFinishRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductionFinishResponse:
    result = production_run_service.finish_production(
        db,
        business,
        production_run_id,
        expected_version=payload.version,
        actual_output_quantity=payload.actual_output_quantity,
        ingredient_actual_quantities=payload.ingredient_actual_quantities,
        business_today=_business_today(business),
    )
    return ProductionFinishResponse(
        run=_run_to_response(result.run, db=db, business=business),
        negative_inventory_ingredient_ids=[
            str(i) for i in result.negative_inventory_ingredient_ids
        ],
        is_shortfall=result.is_shortfall,
    )


@router.post("/{production_run_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_production_run(
    production_run_id: uuid.UUID,
    payload: ProductionCancelRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductionRunResponse:
    run, _result = production_run_service.cancel_production_run(
        db,
        business,
        production_run_id,
        expected_version=payload.version,
        business_today=_business_today(business),
    )
    return _run_to_response(run, db=db, business=business)
