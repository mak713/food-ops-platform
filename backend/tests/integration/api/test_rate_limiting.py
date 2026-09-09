"""Rate limiting on login/signup/password-reset-request (Phase 2 plan §12): approved
thresholds (10/min, 10/min, 5/hour), IP-keyed, not trusting X-Forwarded-For."""

from tests.integration.api.helpers import ORIGIN_HEADERS, signup_payload


def test_login_rate_limited_after_ten_requests_per_minute(client):
    body = {"email": "nobody-in-particular@example.com", "password": "irrelevant-value-here"}
    for _ in range(10):
        response = client.post("/api/v1/auth/login", json=body, headers=ORIGIN_HEADERS)
        assert response.status_code == 401

    eleventh = client.post("/api/v1/auth/login", json=body, headers=ORIGIN_HEADERS)
    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "RATE_LIMITED"


def test_signup_rate_limited_after_ten_requests_per_minute(client):
    body = signup_payload()
    for _ in range(10):
        client.post("/api/v1/auth/signup", json=body, headers=ORIGIN_HEADERS)

    eleventh = client.post("/api/v1/auth/signup", json=body, headers=ORIGIN_HEADERS)
    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "RATE_LIMITED"


def test_password_reset_request_rate_limited_after_five_requests_per_hour(client):
    body = {"email": "not-registered-either@example.com"}
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/password/reset/request", json=body, headers=ORIGIN_HEADERS
        )
        assert response.status_code == 200

    sixth = client.post("/api/v1/auth/password/reset/request", json=body, headers=ORIGIN_HEADERS)
    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_key_ignores_forwarded_for_header(client):
    """The limit must be keyed by the real socket peer address, not a client-settable
    X-Forwarded-For header — otherwise it would be trivially spoofable."""
    body = {"email": "still-nobody@example.com", "password": "irrelevant-value-here"}
    for i in range(10):
        headers = {**ORIGIN_HEADERS, "X-Forwarded-For": f"203.0.113.{i}"}
        response = client.post("/api/v1/auth/login", json=body, headers=headers)
        assert response.status_code == 401

    eleventh = client.post(
        "/api/v1/auth/login",
        json=body,
        headers={**ORIGIN_HEADERS, "X-Forwarded-For": "203.0.113.250"},
    )
    assert eleventh.status_code == 429
