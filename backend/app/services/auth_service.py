"""Signup / login / logout / password-change / password-reset business logic.

Kept out of the route layer per `CLAUDE.md` ("controllers/routes should remain thin;
business workflows belong in services"). Each mutating function here owns its own
transaction boundary (explicit `db.commit()`/`db.rollback()`) — there is no
request-scoped auto-commit middleware in this codebase (`app/db/session.py`'s `get_db()`
only opens and closes a session), so Phase 2 establishes that convention.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import security
from app.core.api_errors import ApiError
from app.core.mail import Mailer
from app.db.models.auth import AuthSession, PasswordResetToken
from app.db.models.business import Business
from app.db.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ConfirmPasswordResetRequest,
    LoginRequest,
    RequestPasswordResetRequest,
    SignupRequest,
)

# Business columns required NOT NULL by Spec §8.4 but not part of the AUTH-001 signup
# field list (name/email/password/business name/timezone only) — Business Settings CRUD
# to let the owner change these is out of Phase 2 scope, so signup seeds sensible V1
# defaults (SET-004 allows either 3 or 7 days; 7 is the more generous default).
_DEFAULT_FULFILLMENT_WARNING_MINUTES = 60
_DEFAULT_SHOPPING_HORIZON_DAYS = 7


@dataclass
class IssuedAuth:
    user: User
    business: Business
    session: AuthSession
    raw_session_token: str
    raw_csrf_token: str


def _issue_session(db: Session, user: User) -> tuple[AuthSession, str, str]:
    now = datetime.now(UTC)
    raw_session_token = security.generate_token()
    raw_csrf_token = security.generate_token()
    session_row = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=security.hash_token(raw_session_token),
        csrf_token_hash=security.hash_token(raw_csrf_token),
        last_seen_at=now,
        expires_at=now + security.SESSION_IDLE_TTL,
    )
    db.add(session_row)
    return session_row, raw_session_token, raw_csrf_token


def signup(db: Session, payload: SignupRequest) -> IssuedAuth:
    """Creates one User + one Business + an initial (auto-login) Session, all within one
    transaction (AUTH-006, Phase 2 plan §6 amendment) — nothing is persisted, and no
    cookies are ever set by the caller, unless the whole thing commits successfully."""
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise ApiError(409, "AUTH_EMAIL_ALREADY_REGISTERED", "This email is already registered.")

    user = User(
        id=uuid.uuid4(),
        name=payload.name,
        email=payload.email,
        password_hash=security.hash_password(payload.password),
    )
    business = Business(
        id=uuid.uuid4(),
        owner_user_id=user.id,
        name=payload.business_name,
        timezone=payload.business_timezone,
        fulfillment_warning_minutes=_DEFAULT_FULFILLMENT_WARNING_MINUTES,
        shopping_horizon_days=_DEFAULT_SHOPPING_HORIZON_DAYS,
    )
    db.add(user)
    db.add(business)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # The race case: two concurrent signups both passed the pre-check above before
        # either committed. The DB's own unique constraint on users.email is what's
        # actually authoritative here — this branch just translates it into the same
        # response shape as the common pre-check path.
        raise ApiError(
            409, "AUTH_EMAIL_ALREADY_REGISTERED", "This email is already registered."
        ) from None

    # Issued only after the user row is flushed: AuthSession has no ORM `relationship()`
    # to User (kept out deliberately — see app/db/models/auth.py — so as not to touch
    # frozen Phase 1 model files), so SQLAlchemy's automatic flush-ordering can't infer
    # that `users` must be inserted before `sessions` when both are new in one flush.
    # Flushing user/business first guarantees the FK is satisfiable before the session
    # row is even constructed. The whole thing still commits as one transaction below —
    # nothing is durable until then, so the atomicity guarantee is unaffected.
    session_row, raw_session_token, raw_csrf_token = _issue_session(db, user)
    db.commit()
    return IssuedAuth(user, business, session_row, raw_session_token, raw_csrf_token)


def login(db: Session, payload: LoginRequest) -> IssuedAuth:
    """Identical `AUTH_INVALID_CREDENTIALS` response for an unknown email and a known
    email with the wrong password, including comparable Argon2id work in both cases
    (Phase 2 plan §6a) so the two aren't distinguishable by response latency."""
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None:
        security.verify_against_dummy_hash(payload.password)
        raise ApiError(401, "AUTH_INVALID_CREDENTIALS", "Incorrect email or password.")

    if not security.verify_password(payload.password, user.password_hash):
        raise ApiError(401, "AUTH_INVALID_CREDENTIALS", "Incorrect email or password.")

    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(payload.password)

    business = db.scalar(select(Business).where(Business.owner_user_id == user.id))
    if business is None:
        raise RuntimeError("User has no associated Business — violates the signup invariant.")

    session_row, raw_session_token, raw_csrf_token = _issue_session(db, user)
    db.commit()
    return IssuedAuth(user, business, session_row, raw_session_token, raw_csrf_token)


def logout(db: Session, session_row: AuthSession) -> None:
    session_row.revoked_at = datetime.now(UTC)
    db.commit()


def change_password(db: Session, user: User, payload: ChangePasswordRequest) -> IssuedAuth:
    """Verifies `current_password` before anything else changes (Phase 2 plan §8
    amendment); only on success does it hash the new password, revoke every session for
    this user (including the one making the request), and issue a fresh session+CSRF pair
    so the current browser stays logged in with rotated credentials."""
    if not security.verify_password(payload.current_password, user.password_hash):
        raise ApiError(401, "AUTH_INVALID_CREDENTIALS", "Current password is incorrect.")

    user.password_hash = security.hash_password(payload.new_password)

    now = datetime.now(UTC)
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    session_row, raw_session_token, raw_csrf_token = _issue_session(db, user)

    business = db.scalar(select(Business).where(Business.owner_user_id == user.id))
    if business is None:
        raise RuntimeError("User has no associated Business — violates the signup invariant.")

    db.commit()
    return IssuedAuth(user, business, session_row, raw_session_token, raw_csrf_token)


def request_password_reset(
    db: Session, mailer: Mailer, payload: RequestPasswordResetRequest
) -> None:
    """Always looks identical from the outside regardless of whether the email is
    registered (Spec §10.7) — the caller (route) returns the same generic response either
    way; this function simply does nothing observable when there's no matching user."""
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None:
        return

    raw_token = security.generate_token()
    reset_row = PasswordResetToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=security.hash_token(raw_token),
        expires_at=datetime.now(UTC) + security.PASSWORD_RESET_TOKEN_TTL,
    )
    db.add(reset_row)
    db.commit()

    mailer.send(
        to=user.email,
        subject="Reset your Food Operations Platform password",
        body=(
            "Use this token to reset your password within the next 30 minutes:\n\n"
            f"{raw_token}\n\n"
            "If you didn't request this, you can ignore this message."
        ),
    )


def confirm_password_reset(db: Session, payload: ConfirmPasswordResetRequest) -> None:
    """One atomic, row-locked transaction (Phase 2 plan §8): `SELECT ... FOR UPDATE` on
    the token row makes two concurrent confirmations of the same token race-safe — the
    second transaction blocks until the first commits, then sees `used_at` already set
    and fails cleanly instead of both succeeding."""
    token_hash = security.hash_token(payload.token)
    reset_row = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .with_for_update()
    )

    now = datetime.now(UTC)
    if reset_row is None or reset_row.used_at is not None or reset_row.expires_at <= now:
        raise ApiError(
            422,
            "AUTH_RESET_TOKEN_INVALID",
            "This password reset link is invalid or has expired.",
        )

    user = db.get(User, reset_row.user_id)
    if user is None:
        raise RuntimeError("Password reset token references a user that no longer exists.")

    user.password_hash = security.hash_password(payload.new_password)
    reset_row.used_at = now
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()
