"""Business Settings and owner-profile endpoints (Phase 11).

Thin router. The Business and User are always derived from the authenticated session via
`get_current_business` / `get_current_user` (SEC-001/002) — there is no id in any path,
query or body that selects a tenant. Mutations require the full CSRF check.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, get_current_user, require_csrf
from app.db.models.business import Business
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.auth import UserSummary
from app.schemas.settings import (
    BusinessSettingsResponse,
    BusinessSettingsUpdateRequest,
    OwnerProfileUpdateRequest,
)
from app.services import settings_service

router = APIRouter()


@router.get("")
def get_settings(
    business: Annotated[Business, Depends(get_current_business)],
) -> BusinessSettingsResponse:
    return settings_service.to_settings_response(business)


@router.patch("/business", dependencies=[Depends(require_csrf)])
def update_business_settings(
    payload: BusinessSettingsUpdateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> BusinessSettingsResponse:
    return settings_service.update_business_settings(db, business, payload)


@router.patch("/owner", dependencies=[Depends(require_csrf)])
def update_owner_profile(
    payload: OwnerProfileUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UserSummary:
    return settings_service.update_owner_profile(db, user, payload)
