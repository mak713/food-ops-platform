"""Full account/business deletion — session + CSRF + current password + exact business
name confirmation, all enforced before `account_service.delete_account` touches the
database (Phase 2 plan §7)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.cookies import clear_session_cookies
from app.api.deps import get_current_business, get_current_user, require_csrf
from app.db.models.business import Business
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.auth import DeleteAccountRequest
from app.services import account_service

router = APIRouter()


@router.delete("/account", dependencies=[Depends(require_csrf)])
def delete_account(
    payload: DeleteAccountRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    business: Annotated[Business, Depends(get_current_business)],
) -> dict:
    account_service.delete_account(db, user, business, payload)
    clear_session_cookies(response)
    return {"status": "ok"}
