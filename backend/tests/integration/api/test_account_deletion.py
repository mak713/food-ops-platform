"""DELETE /api/v1/account — Phase 2 plan §7: session + CSRF + current password + exact
business name confirmation, atomic, no orphaned rows."""

from sqlalchemy import select

from app.db.models.business import Business
from app.db.models.user import User
from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup_payload


def test_delete_requires_csrf(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)

    response = client.request(
        "DELETE",
        "/api/v1/account",
        json={
            "current_password": payload["password"],
            "business_name_confirmation": "Test Bakery",
        },
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 403


def test_delete_requires_correct_current_password(client, session):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)

    response = client.request(
        "DELETE",
        "/api/v1/account",
        json={
            "current_password": "totally-wrong-password",
            "business_name_confirmation": "Test Bakery",
        },
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"

    remaining = session.execute(select(User).where(User.email == payload["email"])).scalars().all()
    assert len(remaining) == 1


def test_delete_requires_exact_business_name(client, session):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)

    response = client.request(
        "DELETE",
        "/api/v1/account",
        json={
            "current_password": payload["password"],
            "business_name_confirmation": "Not The Right Name",
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ACCOUNT_DELETE_CONFIRMATION_MISMATCH"

    remaining = session.execute(select(User).where(User.email == payload["email"])).scalars().all()
    assert len(remaining) == 1


def test_successful_delete_removes_user_and_business(client, session):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)

    response = client.request(
        "DELETE",
        "/api/v1/account",
        json={
            "current_password": payload["password"],
            "business_name_confirmation": "Test Bakery",
        },
        headers=headers,
    )
    assert response.status_code == 200

    remaining_users = (
        session.execute(select(User).where(User.email == payload["email"])).scalars().all()
    )
    assert remaining_users == []
    remaining_businesses = (
        session.execute(select(Business).where(Business.name == "Test Bakery")).scalars().all()
    )
    assert remaining_businesses == []


def test_successful_delete_clears_session_cookies_and_logs_out(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)

    client.request(
        "DELETE",
        "/api/v1/account",
        json={
            "current_password": payload["password"],
            "business_name_confirmation": "Test Bakery",
        },
        headers=headers,
    )

    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
