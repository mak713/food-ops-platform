"""Regression coverage for two Checkpoint 2 review findings:

1. The sliding 7-day idle expiry (capped at the 30-day absolute lifetime) must be
   *durably* persisted by an authenticated request, not merely flushed into a
   transaction that a subsequent `Session.close()` then rolls back — a real defect in
   `get_current_session()` (it called `db.flush()`, not `db.commit()`) that made a
   read-only request like `GET /me` silently fail to extend the session.
2. The `fo_session`/`fo_csrf` cookies' `Max-Age` must reflect the 30-day absolute cap, not
   the session's *current* 7-day sliding idle deadline — otherwise the browser stops
   sending the cookie after ~7 days regardless of activity, capping every session at 7
   days in practice and never letting an active session reach the approved 30-day cap.
"""

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import security
from app.db.models.auth import AuthSession
from app.db.models.business import Business
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.schemas.auth import SignupRequest
from app.services import auth_service
from tests.integration.api.helpers import ORIGIN_HEADERS, signup_payload, unique_email

_THIRTY_DAYS_SECONDS = int(timedelta(days=30).total_seconds())
_SEVEN_DAYS_SECONDS = int(timedelta(days=7).total_seconds())


def _max_age_of(set_cookie_headers: list[str], cookie_name: str) -> int:
    header = next(h for h in set_cookie_headers if h.startswith(f"{cookie_name}="))
    match = re.search(r"Max-Age=(\d+)", header, re.IGNORECASE)
    assert match, f"no Max-Age found on {cookie_name} cookie: {header!r}"
    return int(match.group(1))


def test_session_and_csrf_cookie_max_age_reflect_the_absolute_cap(client):
    """Issue 2: cookie Max-Age must be ~30 days, not ~7 — decoupled from the session's
    current sliding idle deadline."""
    response = client.post("/api/v1/auth/signup", json=signup_payload(), headers=ORIGIN_HEADERS)
    assert response.status_code == 201

    set_cookie_headers = response.headers.get_list("set-cookie")
    session_max_age = _max_age_of(set_cookie_headers, "fo_session")
    csrf_max_age = _max_age_of(set_cookie_headers, "fo_csrf")

    assert session_max_age > _SEVEN_DAYS_SECONDS
    assert abs(session_max_age - _THIRTY_DAYS_SECONDS) < 60
    assert abs(csrf_max_age - _THIRTY_DAYS_SECONDS) < 60


def test_login_cookie_max_age_also_reflects_the_absolute_cap(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200

    session_max_age = _max_age_of(response.headers.get_list("set-cookie"), "fo_session")
    assert abs(session_max_age - _THIRTY_DAYS_SECONDS) < 60


def test_sliding_expiry_refresh_durably_persists_across_a_fresh_connection(
    real_session_factory,
):
    """Issue 1: drives a real authenticated GET request (through the actual app and
    `get_current_session` dependency, not a reimplementation of its logic) using its own
    independently-committing database session — the same shape of session the real app
    uses — then verifies the refreshed expiry from a completely separate, fresh
    connection. A `db.flush()`-only bug would show the update within the *same*
    transaction but lose it the moment that connection closes; this test only passes if
    the update was actually committed."""
    db_signup = real_session_factory()
    email = unique_email("durability")
    try:
        issued = auth_service.signup(
            db_signup,
            SignupRequest(
                name="Durability Test",
                email=email,
                password="correct horse battery staple",
                business_name="Durability Bakery",
                business_timezone="America/New_York",
            ),
        )
        raw_token = issued.raw_session_token
        session_id = issued.session.id
        user_id = issued.user.id
    finally:
        db_signup.close()

    # Backdate the row so a subsequent refresh is observable (otherwise "already ~7 days
    # out" could mask a no-op).
    backdated_expiry = datetime.now(UTC) + timedelta(hours=1)
    db_backdate = real_session_factory()
    try:
        row = db_backdate.get(AuthSession, session_id)
        row.expires_at = backdated_expiry
        row.last_seen_at = datetime.now(UTC) - timedelta(days=2)
        db_backdate.commit()
    finally:
        db_backdate.close()

    # "A fresh request": the real app, wired to its own independently-committing
    # session — not the savepoint-wrapped per-test `session` fixture, which would mask
    # exactly this defect (its "commit" is only a savepoint release inside a transaction
    # this test's teardown rolls back regardless of whether the code under test commits).
    db_request = real_session_factory()
    app.dependency_overrides[get_db] = lambda: db_request
    app.state.limiter.reset()
    try:
        with TestClient(app) as request_client:
            request_client.cookies.set("fo_session", raw_token)
            response = request_client.get("/api/v1/auth/me")
            assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
        db_request.close()

    # Verify from a brand-new, independent connection.
    db_verify = real_session_factory()
    try:
        row = db_verify.get(AuthSession, session_id)
        assert row.expires_at > backdated_expiry
        assert row.expires_at <= row.created_at + security.SESSION_ABSOLUTE_TTL
    finally:
        db_verify.close()

    db_cleanup = real_session_factory()
    try:
        user = db_cleanup.get(User, user_id)
        if user is not None:
            business = db_cleanup.scalar(select(Business).where(Business.owner_user_id == user.id))
            if business is not None:
                db_cleanup.delete(business)
                db_cleanup.flush()
            db_cleanup.delete(user)
            db_cleanup.commit()
    finally:
        db_cleanup.close()
