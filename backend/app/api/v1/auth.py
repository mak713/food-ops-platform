"""Signup, login, logout, current-user context, password change, and password reset.

Route handlers stay thin — validation lives in the Pydantic schemas, business logic lives
in `app.services.auth_service`, and this module's job is just wiring: dependencies,
cookies, and shaping the response.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.cookies import clear_session_cookies, set_session_cookies
from app.api.deps import (
    get_current_business,
    get_current_session,
    get_current_user,
    require_csrf,
    require_origin_check,
)
from app.core.mail import Mailer, get_mailer
from app.core.rate_limit import (
    LOGIN_RATE_LIMIT,
    PASSWORD_RESET_REQUEST_RATE_LIMIT,
    SIGNUP_RATE_LIMIT,
    limiter,
)
from app.db.models.auth import AuthSession
from app.db.models.business import Business
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.auth import (
    BusinessSummary,
    ChangePasswordRequest,
    ConfirmPasswordResetRequest,
    LoginRequest,
    MeResponse,
    RequestPasswordResetRequest,
    SignupRequest,
    UserSummary,
)
from app.services import auth_service

router = APIRouter()


def _me_response(issued: auth_service.IssuedAuth) -> MeResponse:
    return MeResponse(
        user=UserSummary(id=str(issued.user.id), name=issued.user.name, email=issued.user.email),
        business=BusinessSummary(
            id=str(issued.business.id), name=issued.business.name, timezone=issued.business.timezone
        ),
    )


@router.post("/signup", status_code=201, dependencies=[Depends(require_origin_check)])
@limiter.limit(SIGNUP_RATE_LIMIT)
def signup(
    request: Request,
    payload: SignupRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    issued = auth_service.signup(db, payload)
    set_session_cookies(
        response,
        raw_session_token=issued.raw_session_token,
        raw_csrf_token=issued.raw_csrf_token,
    )
    return _me_response(issued)


@router.post("/login", dependencies=[Depends(require_origin_check)])
@limiter.limit(LOGIN_RATE_LIMIT)
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    issued = auth_service.login(db, payload)
    set_session_cookies(
        response,
        raw_session_token=issued.raw_session_token,
        raw_csrf_token=issued.raw_csrf_token,
    )
    return _me_response(issued)


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    session_row: Annotated[AuthSession, Depends(get_current_session)],
) -> dict:
    auth_service.logout(db, session_row)
    clear_session_cookies(response)
    return {"status": "ok"}


@router.get("/me")
def me(
    user: Annotated[User, Depends(get_current_user)],
    business: Annotated[Business, Depends(get_current_business)],
) -> MeResponse:
    return MeResponse(
        user=UserSummary(id=str(user.id), name=user.name, email=user.email),
        business=BusinessSummary(
            id=str(business.id), name=business.name, timezone=business.timezone
        ),
    )


@router.post("/password/change", dependencies=[Depends(require_csrf)])
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    issued = auth_service.change_password(db, user, payload)
    set_session_cookies(
        response,
        raw_session_token=issued.raw_session_token,
        raw_csrf_token=issued.raw_csrf_token,
    )
    return {"status": "ok"}


@router.post("/password/reset/request", dependencies=[Depends(require_origin_check)])
@limiter.limit(PASSWORD_RESET_REQUEST_RATE_LIMIT)
def request_password_reset(
    request: Request,
    payload: RequestPasswordResetRequest,
    db: Annotated[Session, Depends(get_db)],
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> dict:
    auth_service.request_password_reset(db, mailer, payload)
    # Identical response regardless of whether the email is registered (Spec §10.7).
    return {"status": "ok", "message": "If that email is registered, a reset link has been sent."}


@router.post("/password/reset/confirm", dependencies=[Depends(require_origin_check)])
def confirm_password_reset(
    request: Request,
    payload: ConfirmPasswordResetRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    auth_service.confirm_password_reset(db, payload)
    return {"status": "ok"}
