"""POST /api/v1/auth/password/change — Phase 2 plan §8 amendment: verify current_password
first, then rotate every session (including the one making the request)."""

from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup_payload


def test_change_password_requires_current_password(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)

    response = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": "totally-wrong-password", "new_password": "a" * 20},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_wrong_current_password_changes_nothing(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)
    old_session_cookie = client.cookies.get("fo_session")

    client.post(
        "/api/v1/auth/password/change",
        json={"current_password": "totally-wrong-password", "new_password": "a" * 20},
        headers=headers,
    )

    # The original session must still work — nothing was rotated on a failed attempt.
    assert client.cookies.get("fo_session") == old_session_cookie
    me_response = client.get("/api/v1/auth/me")
    assert me_response.status_code == 200

    # And the old password must still be the one that logs in.
    client.cookies.clear()
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
        headers=ORIGIN_HEADERS,
    )
    assert login_response.status_code == 200


def test_successful_change_rotates_session_and_stays_logged_in(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)
    old_session_cookie = client.cookies.get("fo_session")
    old_csrf_cookie = client.cookies.get("fo_csrf")

    response = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": payload["password"], "new_password": "a-brand-new-password-1"},
        headers=headers,
    )
    assert response.status_code == 200

    new_session_cookie = client.cookies.get("fo_session")
    new_csrf_cookie = client.cookies.get("fo_csrf")
    assert new_session_cookie and new_session_cookie != old_session_cookie
    assert new_csrf_cookie and new_csrf_cookie != old_csrf_cookie

    # The current browser is still logged in with the new cookies.
    me_response = client.get("/api/v1/auth/me")
    assert me_response.status_code == 200


def test_old_session_cookie_is_dead_after_password_change(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)
    old_session_cookie = client.cookies.get("fo_session")

    client.post(
        "/api/v1/auth/password/change",
        json={"current_password": payload["password"], "new_password": "a-brand-new-password-1"},
        headers=headers,
    )

    # Force the old (now-revoked) session cookie back onto the client and confirm it's dead.
    client.cookies.set("fo_session", old_session_cookie)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_new_password_required_to_meet_policy(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)

    response = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": payload["password"], "new_password": "short"},
        headers=headers,
    )
    assert response.status_code == 422


def test_change_password_requires_csrf(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)

    response = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": payload["password"], "new_password": "a-brand-new-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"


def test_login_with_new_password_after_change(client):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    headers = csrf_headers(client)
    client.post(
        "/api/v1/auth/password/change",
        json={"current_password": payload["password"], "new_password": "a-brand-new-password-1"},
        headers=headers,
    )
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "a-brand-new-password-1"},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200
