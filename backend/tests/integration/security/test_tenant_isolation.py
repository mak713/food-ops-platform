"""The real Spec §19.14 tenant-isolation suite Phase 2 establishes — scoped to what Phase
2 can actually prove (Phase 2 plan §13): `User`/`Business` session/context derivation
cannot be influenced by client-supplied tenant data, and forged/tampered/expired/revoked
sessions never authenticate as any business. At least two independently created Businesses
are used throughout, per Spec §19.14/§19.18's minimum.

Phase 2 has no client-facing tenant-owned resource reachable by ID yet (`Business` is only
ever resolved via the session), so the full "foreign-resource-ID -> non-revealing 404"
proof is explicitly deferred to Phase 3, once such a resource exists — see
app/core/tenant.py and tests/unit/test_tenant_helper.py for the reusable pattern Phase 2
hands it, and tests/integration/schema/test_tenant_scoping.py's docstring for the same
pointer at the schema level.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core import security
from app.db.models.auth import AuthSession
from tests.integration.api.helpers import csrf_headers, signup


def _signup_two_businesses(client):
    body_a = signup(client, email="tenant-a@example.com")
    session_a = client.cookies.get("fo_session")
    csrf_a = client.cookies.get("fo_csrf")

    client.cookies.clear()
    body_b = signup(client, email="tenant-b@example.com")
    session_b = client.cookies.get("fo_session")
    csrf_b = client.cookies.get("fo_csrf")

    assert body_a["business"]["id"] != body_b["business"]["id"]
    return {
        "a": {"body": body_a, "session": session_a, "csrf": csrf_a},
        "b": {"body": body_b, "session": session_b, "csrf": csrf_b},
    }


def _use_session(client, which: dict) -> None:
    client.cookies.clear()
    client.cookies.set("fo_session", which["session"])
    client.cookies.set("fo_csrf", which["csrf"])


def test_business_id_query_param_is_ignored(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])

    response = client.get(f"/api/v1/auth/me?business_id={tenants['b']['body']['business']['id']}")
    assert response.status_code == 200
    assert response.json()["business"]["id"] == tenants["a"]["body"]["business"]["id"]


def test_business_id_header_is_ignored(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])

    response = client.get(
        "/api/v1/auth/me", headers={"X-Business-Id": tenants["b"]["body"]["business"]["id"]}
    )
    assert response.status_code == 200
    assert response.json()["business"]["id"] == tenants["a"]["body"]["business"]["id"]


def test_business_a_session_never_resolves_to_business_b_data(client):
    tenants = _signup_two_businesses(client)

    _use_session(client, tenants["a"])
    response_a = client.get("/api/v1/auth/me")
    assert response_a.status_code == 200
    assert response_a.json() == tenants["a"]["body"]

    _use_session(client, tenants["b"])
    response_b = client.get("/api/v1/auth/me")
    assert response_b.status_code == 200
    assert response_b.json() == tenants["b"]["body"]


def test_forged_session_token_is_rejected(client):
    tenants = _signup_two_businesses(client)
    client.cookies.clear()
    client.cookies.set("fo_session", "a-session-token-that-was-never-issued-by-the-server")

    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_UNAUTHENTICATED"
    assert tenants  # both businesses exist; forged token still can't reach either


def test_tampered_session_token_is_rejected(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    valid_token = client.cookies.get("fo_session")
    tampered = valid_token[:-1] + ("a" if valid_token[-1] != "a" else "b")
    client.cookies.set("fo_session", tampered)

    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_expired_session_is_rejected(client, session):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])

    token_hash = security.hash_token(tenants["a"]["session"])
    row = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    session.commit()

    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_revoked_session_is_rejected(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])

    headers = csrf_headers(client)
    logout_response = client.post("/api/v1/auth/logout", headers=headers)
    assert logout_response.status_code == 200

    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
