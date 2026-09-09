"""Dedicated coverage for the five-part CSRF check and the Origin/Referer allow-list
(Phase 2 plan §3), beyond what individual endpoint test files already exercise in passing.
"""

from urllib.parse import urlsplit

from app.core.config import get_settings
from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup, signup_payload

_FRONTEND_ORIGIN = get_settings().frontend_origin
_PARSED = urlsplit(_FRONTEND_ORIGIN)
_DIFFERENT_PORT_ORIGIN = f"{_PARSED.scheme}://{_PARSED.hostname}:{(_PARSED.port or 80) + 1}"


def test_mutating_request_without_csrf_cookie_rejected(client):
    signup(client)
    token = client.cookies.get("fo_csrf")
    client.cookies.delete("fo_csrf")

    response = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": token, **ORIGIN_HEADERS})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"


def test_mutating_request_without_csrf_header_rejected(client):
    signup(client)
    response = client.post("/api/v1/auth/logout", headers=ORIGIN_HEADERS)
    assert response.status_code == 403


def test_mutating_request_with_mismatched_cookie_and_header_rejected(client):
    signup(client)
    response = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": "a-value-that-does-not-match-the-cookie", **ORIGIN_HEADERS},
    )
    assert response.status_code == 403


def test_csrf_token_from_a_different_session_is_rejected(client):
    """Proves the check is session-*bound*, not just double-submit: a syntactically valid
    cookie==header pair that was issued for a *different* session must still fail."""
    signup(client, email="session-one@example.com")
    session_one_cookie = client.cookies.get("fo_session")

    client.cookies.clear()
    signup(client, email="session-two@example.com")
    session_two_csrf = client.cookies.get("fo_csrf")

    # Frankenstein request: session one's session cookie, session two's CSRF cookie/header.
    client.cookies.set("fo_session", session_one_cookie)
    client.cookies.set("fo_csrf", session_two_csrf)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": session_two_csrf, **ORIGIN_HEADERS},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"


def test_get_requests_are_exempt_from_csrf(client):
    signup(client)
    client.cookies.delete("fo_csrf")
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200


def test_valid_csrf_and_origin_succeeds(client):
    signup(client)
    headers = csrf_headers(client)
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200


def test_origin_null_is_rejected(client):
    signup(client)
    headers = csrf_headers(client)
    headers["Origin"] = "null"
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 403


def test_origin_with_different_port_is_rejected(client):
    signup(client)
    headers = csrf_headers(client)
    headers["Origin"] = _DIFFERENT_PORT_ORIGIN
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 403


def test_referer_with_path_is_normalized_to_its_origin(client):
    """Referer normally carries a path — the check must compare only the parsed origin,
    not do a raw string match (Phase 2 plan §3 amendment)."""
    signup(client)
    csrf_token = client.cookies.get("fo_csrf")
    headers = {
        "X-CSRF-Token": csrf_token,
        "Referer": _FRONTEND_ORIGIN.rstrip("/") + "/some/deep/page?query=1",
    }
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200


def test_referer_from_a_foreign_origin_is_rejected(client):
    signup(client)
    csrf_token = client.cookies.get("fo_csrf")
    headers = {
        "X-CSRF-Token": csrf_token,
        "Referer": "https://attacker.example.com/some/page",
    }
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 403


def test_signup_and_login_protected_by_origin_check_only(client):
    """Pre-session endpoints have no CSRF token yet, but are not unprotected — they get
    the Origin/Referer allow-list check (Phase 2 plan §3)."""
    signup_response = client.post("/api/v1/auth/signup", json=signup_payload())
    assert signup_response.status_code == 403
    assert signup_response.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"

    login_response = client.post(
        "/api/v1/auth/login", json={"email": "someone@example.com", "password": "irrelevant-here"}
    )
    assert login_response.status_code == 403
