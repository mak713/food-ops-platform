"""Request/response schemas for manual Surplus creation and adjustment (SUR-008;
Phase 8 Final Architecture Lock §9, corrected by Final Architecture Amendment §3 and
Final Architecture Lock Addendum §B)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.inventory import ManualAdjustmentReason


class SurplusManualCreateRequest(BaseModel):
    """A new, manually-originated lot — `source_production_run_id` is always NULL for
    this path (a run-sourced lot is only ever created by Finish Production). The
    seller-required `unit_cost_basis` is never silently defaulted; the caller/frontend
    may pre-fill a suggested value, but it must always be explicitly present and
    seller-visible in the submitted request."""

    product_id: uuid.UUID
    quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    unit_cost_basis: Decimal = Field(max_digits=18, decimal_places=6, ge=0)
    produced_at: datetime | None = None
    usable_through_date: date | None = None
    is_reusable: bool | None = None
    notes: str | None = None


class SurplusAdjustmentRequest(BaseModel):
    """Reduction-only (Phase 8 Final Architecture Amendment §3/Lock Addendum §B) —
    `quantity_delta` must be negative. New positive supply always uses
    `SurplusManualCreateRequest` (`POST /surplus`), never an in-place top-up."""

    version: int
    quantity_delta: Decimal = Field(max_digits=18, decimal_places=6, lt=0)
    reason: ManualAdjustmentReason
    notes: str | None = None
    acknowledged_warning_fingerprint: str | None = None
