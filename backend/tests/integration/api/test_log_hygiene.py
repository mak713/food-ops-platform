"""Proves passwords, session tokens, CSRF tokens, and password-reset tokens never appear
in application log output across a full signup -> login -> reset flow (Phase 2 plan §11;
Spec §11.10). Uses InMemoryMailer specifically so nothing is written anywhere but memory —
there is no "safe, intentional" log write of the reset link to reconcile with."""

import logging

from tests.integration.api.helpers import ORIGIN_HEADERS, signup_payload


def _extract_token(mailer) -> str:
    assert len(mailer.sent) == 1, mailer.sent
    return mailer.sent[0]["body"].split("\n\n")[1].strip()


def test_no_sensitive_values_appear_in_logs(client, mailer, caplog):
    caplog.set_level(logging.DEBUG)

    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    session_token = client.cookies.get("fo_session")
    csrf_token = client.cookies.get("fo_csrf")
    assert session_token and csrf_token

    client.cookies.clear()
    client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
        headers=ORIGIN_HEADERS,
    )

    client.post(
        "/api/v1/auth/password/reset/request",
        json={"email": payload["email"]},
        headers=ORIGIN_HEADERS,
    )
    raw_reset_token = _extract_token(mailer)

    new_password = "a-brand-new-password-after-reset-1"
    client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": raw_reset_token, "new_password": new_password},
        headers=ORIGIN_HEADERS,
    )

    log_text = caplog.text
    assert payload["password"] not in log_text
    assert new_password not in log_text
    assert session_token not in log_text
    assert csrf_token not in log_text
    assert raw_reset_token not in log_text
