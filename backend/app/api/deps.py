"""FastAPI dependencies for authentication, session resolution, and CSRF protection.

`get_current_business` is the seam every Phase 3+ tenant-scoped router will depend on
exactly the way `get_db` is depended on today: it derives the active Business solely from
the authenticated session, never from any client-supplied value, satisfying SEC-002 by
construction rather than by convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core import security
from app.core.api_errors import ApiError
from app.core.config import get_settings
from app.db.models.auth import AuthSession
from app.db.models.business import Business
from app.db.models.user import User
from app.db.session import get_db

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalized_origin(value: str) -> str | None:
    """Parse a URL-ish value and return its normalized `scheme://host:port` origin, or
    None if it isn't a parseable absolute URL with both a scheme and a host. Deliberately
    ignores path/query/fragment, since `Referer` normally carries a path and a naive
    string comparison against it would be bypassable."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    effective_port = port if port is not None else _DEFAULT_PORTS.get(scheme)
    if effective_port is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{effective_port}"


def _check_origin_or_referer(request: Request) -> None:
    """Reject unless `Origin` (preferred) or, failing that, `Referer`'s parsed origin
    matches the configured frontend origin exactly. Runs independently of CSRF-token
    checks — defense-in-depth against double-submit bypass techniques."""
    allowed = _normalized_origin(get_settings().frontend_origin)

    origin_header = request.headers.get("origin")
    if origin_header is not None:
        if origin_header.lower() == "null":
            raise ApiError(403, "AUTH_CSRF_MISMATCH", "Request origin could not be verified.")
        candidate = _normalized_origin(origin_header)
    else:
        referer_header = request.headers.get("referer")
        if not referer_header:
            raise ApiError(403, "AUTH_CSRF_MISMATCH", "Request origin could not be verified.")
        candidate = _normalized_origin(referer_header)

    if candidate is None or allowed is None or candidate != allowed:
        raise ApiError(403, "AUTH_CSRF_MISMATCH", "Request origin could not be verified.")


def require_origin_check(request: Request) -> None:
    """For pre-session endpoints (signup/login) that have no session-bound CSRF token yet.
    Explicitly named/applied rather than left implicit, so it's visibly a deliberate,
    narrower control for these endpoints — not "no protection" (Phase 2 plan §3)."""
    if request.method not in _SAFE_METHODS:
        _check_origin_or_referer(request)


def get_current_session(
    request: Request,
    db: Annotated[OrmSession, Depends(get_db)],
) -> AuthSession:
    raw_token = request.cookies.get(security.SESSION_COOKIE_NAME)
    if not raw_token:
        raise ApiError(401, "AUTH_UNAUTHENTICATED", "Authentication is required.")

    token_hash = security.hash_token(raw_token)
    session_row = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))

    now = datetime.now(UTC)
    if session_row is None or session_row.revoked_at is not None or session_row.expires_at <= now:
        raise ApiError(401, "AUTH_UNAUTHENTICATED", "Your session has expired or is invalid.")

    # Sliding-idle refresh, capped at the 30-day absolute lifetime from creation (Phase 2
    # plan §5) — no separate absolute-expiry column; the cap is computed from created_at.
    # Committed, not just flushed: get_db() has no request-level auto-commit, and
    # Session.close() (in get_db()'s finally) rolls back rather than commits, so a
    # read-only request (e.g. GET /me) would otherwise flush this update into the
    # transaction and then silently lose it when the session closes without ever
    # persisting the refreshed idle deadline (Checkpoint 2 review, issue 1).
    session_row.last_seen_at = now
    session_row.expires_at = min(
        now + security.SESSION_IDLE_TTL,
        session_row.created_at + security.SESSION_ABSOLUTE_TTL,
    )
    db.commit()
    return session_row


def get_current_user(
    session_row: Annotated[AuthSession, Depends(get_current_session)],
    db: Annotated[OrmSession, Depends(get_db)],
) -> User:
    user = db.get(User, session_row.user_id)
    if user is None:
        # The session row outliving its User would only happen if something bypassed the
        # ON DELETE CASCADE FK (Phase 2 plan §9) — not a reachable state in normal
        # operation, so this is treated as an unexpected error, not a coded auth failure.
        raise RuntimeError("Session references a user that no longer exists.")
    return user


def get_current_business(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[OrmSession, Depends(get_db)],
) -> Business:
    """Derives the active Business solely from the authenticated User — never from any
    client-supplied `business_id` (SEC-002). AUTH-006's signup atomicity guarantees every
    User has exactly one Business, so a missing one here is an invariant violation, not a
    normal 404 case."""
    business = db.scalar(select(Business).where(Business.owner_user_id == user.id))
    if business is None:
        raise RuntimeError("User has no associated Business — violates the signup invariant.")
    return business


def require_csrf(
    request: Request,
    session_row: Annotated[AuthSession, Depends(get_current_session)],
) -> None:
    """The full double-submit, session-bound CSRF check for authenticated mutating
    requests (Phase 2 plan §3) — all five checks are required:

    1. the CSRF cookie is present;
    2. the CSRF header is present;
    3. cookie and header are equal (constant-time) — the double-submit half;
    4. sha256(header) matches *this session's own* stored csrf_token_hash — the
       session-bound half, rejecting a syntactically valid pair issued for a different
       session even though check 3 alone would have passed it;
    5. the Origin/Referer allow-list check also passes.
    """
    if request.method in _SAFE_METHODS:
        return

    _check_origin_or_referer(request)

    cookie_token = request.cookies.get(security.CSRF_COOKIE_NAME)
    header_token = request.headers.get(security.CSRF_HEADER_NAME)
    if not cookie_token or not header_token:
        raise ApiError(403, "AUTH_CSRF_MISMATCH", "CSRF validation failed.")
    if not security.constant_time_compare(cookie_token, header_token):
        raise ApiError(403, "AUTH_CSRF_MISMATCH", "CSRF validation failed.")
    if security.hash_token(header_token) != session_row.csrf_token_hash:
        raise ApiError(403, "AUTH_CSRF_MISMATCH", "CSRF validation failed.")
