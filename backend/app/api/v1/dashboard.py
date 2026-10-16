"""Operational Dashboard endpoint (Spec §6.13, §12.15; Phase 9). Thin router per
`CLAUDE.md`/Spec §11.2 — assembly lives in `app.services.dashboard_service`. Read-only:
no CSRF requirement, no locks, no writes. The Business is derived solely from the
authenticated session (SEC-002); no `business_id` parameter exists."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_business
from app.db.models.business import Business
from app.db.session import get_db
from app.schemas.dashboard import DashboardResponse
from app.services import dashboard_service

router = APIRouter()


def _utc_now() -> datetime:
    """The request clock — a module-level function so tests can freeze time
    deterministically (Spec §19.13) without touching the service."""
    return datetime.now(UTC)


def _business_today(business: Business, now_utc: datetime) -> date:
    return now_utc.astimezone(ZoneInfo(business.timezone)).date()


@router.get("")
def get_dashboard(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> DashboardResponse:
    now_utc = _utc_now()
    return dashboard_service.build_dashboard(
        db, business, now_utc=now_utc, business_today=_business_today(business, now_utc)
    )
