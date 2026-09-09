"""POST /api/v1/auth/login, /logout, GET /me — Phase 2 plan §3, §6a."""

from app.core import security
from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup, signup_payload


def test_login_with_correct_credentials_succeeds(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == payload["email"].strip().lower()
    assert client.cookies.get("fo_session")
    assert client.cookies.get("fo_csrf")


def test_login_unknown_email_returns_generic_401(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody-here@example.com", "password": "does-not-matter-at-all"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_login_wrong_password_returns_identical_generic_401(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "the-wrong-password-entirely"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_unknown_email_and_wrong_password_responses_are_identical(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    client.cookies.clear()

    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "still-nobody@example.com", "password": "irrelevant-value-here"},
        headers=ORIGIN_HEADERS,
    )
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "the-wrong-password-entirely"},
        headers=ORIGIN_HEADERS,
    )
    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["error"]["code"] == wrong_password.json()["error"]["code"]
    assert unknown.json()["error"]["message"] == wrong_password.json()["error"]["message"]


def test_login_exercises_dummy_hash_path_for_unknown_email(monkeypatch, client):
    calls = []
    original = security.verify_against_dummy_hash

    def _spy(password: str) -> None:
        calls.append(password)
        return original(password)

    # auth_service.py does `from app.core import security` then calls
    # `security.verify_against_dummy_hash(...)` — patching the attribute on the shared
    # module object here is visible to every caller, auth_service included.
    monkeypatch.setattr(security, "verify_against_dummy_hash", _spy)

    client.post(
        "/api/v1/auth/login",
        json={"email": "definitely-unknown@example.com", "password": "whatever-value-here"},
        headers=ORIGIN_HEADERS,
    )
    assert calls == ["whatever-value-here"]


def test_me_requires_authentication(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_UNAUTHENTICATED"


def test_me_returns_current_user_and_business(client):
    body = signup(client)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json() == body


def test_logout_revokes_session_immediately(client):
    signup(client)
    headers = csrf_headers(client)

    logout_response = client.post("/api/v1/auth/logout", headers=headers)
    assert logout_response.status_code == 200

    me_response = client.get("/api/v1/auth/me")
    assert me_response.status_code == 401


def test_logout_requires_csrf(client):
    signup(client)
    response = client.post("/api/v1/auth/logout", headers=ORIGIN_HEADERS)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"
