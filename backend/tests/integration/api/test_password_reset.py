"""POST /api/v1/auth/password/reset/{request,confirm} — Phase 2 plan §6/§8: generic
non-disclosing request response, single-use race-safe confirmation."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core import security
from app.db.models.auth import AuthSession, PasswordResetToken
from app.db.models.business import Business
from app.db.models.user import User
from app.schemas.auth import ConfirmPasswordResetRequest, SignupRequest
from app.services import auth_service
from tests.integration.api.helpers import ORIGIN_HEADERS, signup_payload


def _extract_token(mailer) -> str:
    assert len(mailer.sent) == 1, mailer.sent
    body = mailer.sent[0]["body"]
    return body.split("\n\n")[1].strip()


def test_request_reset_for_registered_email_sends_mail_and_creates_token(client, session, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200
    assert len(mailer.sent) == 1
    assert mailer.sent[0]["to"] == payload["email"].strip().lower()

    tokens = session.execute(select(PasswordResetToken)).scalars().all()
    assert len(tokens) == 1


def test_request_reset_response_identical_for_unknown_email(client, mailer):
    known_response = client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": "nobody-registered@example.com"},
        headers=ORIGIN_HEADERS,
    )
    assert known_response.status_code == 200
    assert mailer.sent == []
    assert known_response.json() == {
        "status": "ok",
        "message": "If that email is registered, a reset link has been sent.",
    }


def test_request_reset_requires_origin_check(client):
    response = client.post(
        "/api/v1/auth/password/reset/request", json={"email": "someone@example.com"}
    )
    assert response.status_code == 403


def test_reset_token_never_appears_in_api_response(client, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    response = client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)
    assert raw_token not in response.text


def test_confirm_with_valid_token_allows_login_with_new_password(client, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.cookies.clear()
    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)

    confirm_response = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "a-brand-new-reset-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert confirm_response.status_code == 200

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "a-brand-new-reset-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert login_response.status_code == 200

    old_password_login = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
        headers=ORIGIN_HEADERS,
    )
    assert old_password_login.status_code == 401


def test_confirm_invalidates_all_existing_sessions(client, session, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    old_session_cookie = client.cookies.get("fo_session")

    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)
    client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "a-brand-new-reset-password-1"},
        headers=ORIGIN_HEADERS,
    )

    client.cookies.set("fo_session", old_session_cookie)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401

    remaining_active = (
        session.execute(select(AuthSession).where(AuthSession.revoked_at.is_(None))).scalars().all()
    )
    assert remaining_active == []


def test_confirm_with_invalid_token_returns_422(client):
    response = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": "this-token-was-never-issued", "new_password": "a-brand-new-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AUTH_RESET_TOKEN_INVALID"


def test_confirm_with_expired_token_returns_422(client, session, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)

    token_row = session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == security.hash_token(raw_token)
        )
    )
    token_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.flush()
    session.commit()

    response = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "a-brand-new-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AUTH_RESET_TOKEN_INVALID"


def test_confirm_token_is_single_use(client, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)

    first = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "a-brand-new-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "a-second-different-password-2"},
        headers=ORIGIN_HEADERS,
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "AUTH_RESET_TOKEN_INVALID"


def test_new_password_at_confirm_must_meet_policy(client, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)

    response = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "short"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 422


def test_confirm_requires_origin_check(client, mailer):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_token = _extract_token(mailer)

    response = client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_token, "new_password": "a-brand-new-password-1"},
    )
    assert response.status_code == 403


def test_concurrent_confirm_with_same_token_is_race_safe(real_session_factory):
    """Genuine two-connection race (Phase 2 plan §8): SELECT ... FOR UPDATE on the token
    row means the second confirmation blocks until the first commits, then sees used_at
    already set and fails cleanly — never two successful password changes from one token.
    """
    db_setup = real_session_factory()
    try:
        email = "reset-race@example.com"
        signup_result = auth_service.signup(
            db_setup,
            SignupRequest(
                name="Racer",
                email=email,
                password="correct horse battery staple",
                business_name="Race Bakery",
                business_timezone="America/New_York",
            ),
        )
        user_id = signup_result.user.id

        raw_token = security.generate_token()
        reset_row = PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=security.hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        db_setup.add(reset_row)
        db_setup.commit()
    finally:
        db_setup.close()

    results: list[tuple[bool, object]] = []
    barrier = threading.Barrier(2)

    def _attempt(new_password: str) -> None:
        db = real_session_factory()
        confirm_payload = ConfirmPasswordResetRequest(token=raw_token, new_password=new_password)
        barrier.wait(timeout=5)
        try:
            auth_service.confirm_password_reset(db, confirm_payload)
            results.append((True, new_password))
        except Exception as exc:  # noqa: BLE001
            results.append((False, exc))
        finally:
            db.close()

    threads = [
        threading.Thread(target=_attempt, args=("first-attempt-password-1",)),
        threading.Thread(target=_attempt, args=("second-attempt-password-2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        successes = [r for ok, r in results if ok]
        failures = [r for ok, r in results if not ok]
        assert len(successes) == 1, f"expected exactly one confirm to succeed, got {results}"
        assert len(failures) == 1
        assert getattr(failures[0], "code", None) == "AUTH_RESET_TOKEN_INVALID"

        verify_db = real_session_factory()
        try:
            token_row = verify_db.scalar(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_hash == security.hash_token(raw_token)
                )
            )
            assert token_row.used_at is not None
        finally:
            verify_db.close()
    finally:
        cleanup = real_session_factory()
        try:
            user = cleanup.scalar(select(User).where(User.email == email))
            if user is not None:
                business = cleanup.scalar(select(Business).where(Business.owner_user_id == user.id))
                if business is not None:
                    cleanup.delete(business)
                    cleanup.flush()
                cleanup.delete(user)
                cleanup.commit()
        finally:
            cleanup.close()
