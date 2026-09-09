"""POST /api/v1/auth/signup — AUTH-006, Phase 2 plan §6/§6 amendment."""

import threading

from sqlalchemy import select

from app.db.models.business import Business
from app.db.models.user import User
from app.schemas.auth import SignupRequest
from app.services import auth_service
from tests.integration.api.helpers import ORIGIN_HEADERS, signup, signup_payload, unique_email


def test_signup_creates_one_user_and_one_business(client, session):
    body = signup(client)

    assert body["user"]["email"]
    assert body["business"]["name"] == "Test Bakery"

    users = session.execute(select(User).where(User.email == body["user"]["email"])).scalars().all()
    assert len(users) == 1
    businesses = (
        session.execute(select(Business).where(Business.owner_user_id == users[0].id))
        .scalars()
        .all()
    )
    assert len(businesses) == 1


def test_signup_sets_session_and_csrf_cookies(client):
    signup(client)
    assert client.cookies.get("fo_session")
    assert client.cookies.get("fo_csrf")


def test_signup_response_never_contains_password_hash(client):
    response = client.post("/api/v1/auth/signup", json=signup_payload(), headers=ORIGIN_HEADERS)
    assert "password_hash" not in response.text
    assert "password" not in response.json()["user"]


def test_duplicate_email_returns_409(client):
    payload = signup_payload()
    first = client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    assert first.status_code == 201

    second_payload = signup_payload(email=payload["email"])
    second = client.post("/api/v1/auth/signup", json=second_payload, headers=ORIGIN_HEADERS)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "AUTH_EMAIL_ALREADY_REGISTERED"


def test_duplicate_email_case_and_whitespace_insensitive(client):
    payload = signup_payload(email="Owner@Example.com")
    first = client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    assert first.status_code == 201

    second_payload = signup_payload(email="  owner@example.com  ")
    second = client.post("/api/v1/auth/signup", json=second_payload, headers=ORIGIN_HEADERS)
    assert second.status_code == 409


def test_password_below_minimum_length_rejected(client):
    payload = signup_payload(password="short-password")
    response = client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    assert response.status_code == 422


def test_signup_without_origin_or_referer_is_rejected(client):
    response = client.post("/api/v1/auth/signup", json=signup_payload())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"


def test_signup_with_mismatched_origin_is_rejected(client):
    response = client.post(
        "/api/v1/auth/signup",
        json=signup_payload(),
        headers={"Origin": "https://not-the-frontend.example.com"},
    )
    assert response.status_code == 403


def test_concurrent_signup_with_same_email_leaves_exactly_one_user(real_session_factory):
    """Genuine two-connection race (Phase 2 plan §6 amendment): both signups pass the
    pre-check before either commits; the DB's own unique constraint on users.email is
    what's actually authoritative, and the second request must fail cleanly with 409
    rather than 500 or a duplicate row."""
    email = unique_email("race")
    payload = SignupRequest(
        name="Racer One",
        email=email,
        password="correct horse battery staple",
        business_name="Race Bakery",
        business_timezone="America/New_York",
    )

    results: list[tuple[bool, object]] = []
    barrier = threading.Barrier(2)

    def _attempt() -> None:
        db = real_session_factory()
        barrier.wait(timeout=5)
        try:
            issued = auth_service.signup(db, payload)
            results.append((True, issued))
        except Exception as exc:  # noqa: BLE001 - capturing for assertion, not swallowing
            results.append((False, exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    successes = [r for ok, r in results if ok]
    failures = [r for ok, r in results if not ok]

    try:
        assert len(successes) == 1, f"expected exactly one signup to succeed, got {results}"
        assert len(failures) == 1
        assert getattr(failures[0], "code", None) == "AUTH_EMAIL_ALREADY_REGISTERED"

        cleanup = real_session_factory()
        try:
            user = cleanup.scalar(select(User).where(User.email == email.strip().lower()))
            assert user is not None
            matching_users = (
                cleanup.execute(select(User).where(User.email == email.strip().lower()))
                .scalars()
                .all()
            )
            assert len(matching_users) == 1
        finally:
            cleanup.close()
    finally:
        # Clean up regardless of assertion outcome so a failing run doesn't poison
        # TEST_DATABASE_URL for subsequent runs.
        cleanup = real_session_factory()
        try:
            user = cleanup.scalar(select(User).where(User.email == email.strip().lower()))
            if user is not None:
                business = cleanup.scalar(select(Business).where(Business.owner_user_id == user.id))
                if business is not None:
                    cleanup.delete(business)
                    cleanup.flush()
                cleanup.delete(user)
                cleanup.commit()
        finally:
            cleanup.close()
