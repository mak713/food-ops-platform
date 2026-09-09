"""Small shared helpers for the Phase 2 API integration tests — signup/login boilerplate
and CSRF header construction, so individual test files stay focused on what they're
actually verifying."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings

ORIGIN_HEADERS = {"Origin": get_settings().frontend_origin}

_counter = 0


def unique_email(prefix: str = "owner") -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter}@example.com"


def signup_payload(**overrides) -> dict:
    payload = {
        "name": "Test Owner",
        "email": unique_email(),
        "password": "correct horse battery staple",
        "business_name": "Test Bakery",
        "business_timezone": "America/New_York",
    }
    payload.update(overrides)
    return payload


def signup(client: TestClient, **overrides) -> dict:
    payload = signup_payload(**overrides)
    response = client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def csrf_headers(client: TestClient) -> dict:
    token = client.cookies.get("fo_csrf")
    assert token, "expected the fo_csrf cookie to be set on the client"
    return {"X-CSRF-Token": token, **ORIGIN_HEADERS}
