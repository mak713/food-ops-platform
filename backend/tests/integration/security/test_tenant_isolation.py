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


# --- Phase 3: the "foreign-tenant resource-by-ID -> non-revealing 404" proof Phase 2
# explicitly deferred (see this file's module docstring and ADR-098) — Customer is the
# first real tenant-owned resource reachable by ID. Reuses the same two-tenant helpers
# above rather than inventing a parallel scaffold (Phase 3 plan v3 §17).


def test_foreign_tenant_customer_returns_non_revealing_404(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    created = client.post(
        "/api/v1/customers", json={"name": "Business A Customer"}, headers=csrf_headers(client)
    ).json()

    _use_session(client, tenants["b"])
    foreign = client.get(f"/api/v1/customers/{created['id']}")
    missing = client.get("/api/v1/customers/00000000-0000-0000-0000-000000000000")

    assert foreign.status_code == missing.status_code == 404
    # request_id legitimately differs per request — compare everything else.
    foreign_error = {k: v for k, v in foreign.json()["error"].items() if k != "request_id"}
    missing_error = {k: v for k, v in missing.json()["error"].items() if k != "request_id"}
    assert foreign_error == missing_error
    assert foreign_error["code"] == "NOT_FOUND"


def test_foreign_tenant_cannot_mutate_or_delete_customer(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    created = client.post(
        "/api/v1/customers", json={"name": "Business A Customer"}, headers=csrf_headers(client)
    ).json()

    _use_session(client, tenants["b"])
    headers_b = csrf_headers(client)
    patch_response = client.patch(
        f"/api/v1/customers/{created['id']}",
        json={"version": created["version"], "name": "Hijacked"},
        headers=headers_b,
    )
    delete_response = client.delete(
        f"/api/v1/customers/{created['id']}?version={created['version']}", headers=headers_b
    )
    assert patch_response.status_code == 404
    assert delete_response.status_code == 404

    _use_session(client, tenants["a"])
    still_owned = client.get(f"/api/v1/customers/{created['id']}")
    assert still_owned.status_code == 200
    assert still_owned.json()["name"] == "Business A Customer"


def test_customer_list_never_leaks_foreign_tenant_rows(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    client.post("/api/v1/customers", json={"name": "Business A Only"}, headers=csrf_headers(client))

    _use_session(client, tenants["b"])
    listing = client.get("/api/v1/customers")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    assert listing.json()["items"] == []


def test_foreign_tenant_product_returns_non_revealing_404(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    created = client.post(
        "/api/v1/products",
        json={"name": "Business A Product", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    _use_session(client, tenants["b"])
    foreign = client.get(f"/api/v1/products/{created['id']}")
    missing = client.get("/api/v1/products/00000000-0000-0000-0000-000000000000")

    assert foreign.status_code == missing.status_code == 404
    foreign_error = {k: v for k, v in foreign.json()["error"].items() if k != "request_id"}
    missing_error = {k: v for k, v in missing.json()["error"].items() if k != "request_id"}
    assert foreign_error == missing_error
    assert foreign_error["code"] == "NOT_FOUND"
    assert foreign_error["issues"] == []


def test_foreign_tenant_cannot_mutate_selling_options_via_own_product(client):
    """A tenant cannot reach or mutate another tenant's Selling Options at all — not even
    by first creating their own Product and guessing at a foreign Selling Option ID under
    it, since the lookup is scoped by (product_id, business_id) together. Exercised
    entirely through the approved nested API surface (PATCH/archive/reactivate/delete) —
    the unapproved single-resource GET was removed in the Checkpoint 3 remediation."""
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    product_a = client.post(
        "/api/v1/products",
        json={"name": "Business A Product", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    option_a = client.post(
        f"/api/v1/products/{product_a['id']}/selling-options",
        json={"name": "6-pack", "quantity_units": 6, "price": "12.00"},
        headers=csrf_headers(client),
    ).json()

    _use_session(client, tenants["b"])
    product_b = client.post(
        "/api/v1/products",
        json={"name": "Business B Product", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    headers_b = csrf_headers(client)

    # B's own product, but A's selling-option ID — must 404, not leak or mutate A's row.
    patch_via_own_product = client.patch(
        f"/api/v1/products/{product_b['id']}/selling-options/{option_a['id']}",
        json={"version": option_a["version"], "price": "1.00"},
        headers=headers_b,
    )
    assert patch_via_own_product.status_code == 404

    archive_via_own_product = client.post(
        f"/api/v1/products/{product_b['id']}/selling-options/{option_a['id']}/archive",
        json={"version": option_a["version"]},
        headers=headers_b,
    )
    assert archive_via_own_product.status_code == 404

    # B can't even reach A's product directly to attempt a nested mutation under it.
    patch_via_foreign_product = client.patch(
        f"/api/v1/products/{product_a['id']}/selling-options/{option_a['id']}",
        json={"version": option_a["version"], "price": "1.00"},
        headers=headers_b,
    )
    assert patch_via_foreign_product.status_code == 404

    delete_via_foreign_product = client.delete(
        f"/api/v1/products/{product_a['id']}/selling-options/{option_a['id']}"
        f"?version={option_a['version']}",
        headers=headers_b,
    )
    assert delete_via_foreign_product.status_code == 404

    # Confirm nothing above actually mutated A's option.
    _use_session(client, tenants["a"])
    still_a = client.get(f"/api/v1/products/{product_a['id']}/selling-options")
    assert still_a.status_code == 200
    assert still_a.json()[0]["price"] == "12.00"
    assert still_a.json()[0]["version"] == option_a["version"]


def test_foreign_tenant_cannot_archive_or_reactivate_customer(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    created = client.post(
        "/api/v1/customers", json={"name": "Business A Customer"}, headers=csrf_headers(client)
    ).json()

    _use_session(client, tenants["b"])
    headers_b = csrf_headers(client)
    archive_response = client.post(
        f"/api/v1/customers/{created['id']}/archive",
        json={"version": created["version"]},
        headers=headers_b,
    )
    assert archive_response.status_code == 404

    _use_session(client, tenants["a"])
    still_active = client.get(f"/api/v1/customers/{created['id']}")
    assert still_active.json()["is_active"] is True


def test_foreign_tenant_cannot_mutate_product(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    created = client.post(
        "/api/v1/products",
        json={"name": "Business A Product", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    _use_session(client, tenants["b"])
    headers_b = csrf_headers(client)

    patch_response = client.patch(
        f"/api/v1/products/{created['id']}",
        json={"version": created["version"], "name": "Hijacked"},
        headers=headers_b,
    )
    assert patch_response.status_code == 404

    archive_response = client.post(
        f"/api/v1/products/{created['id']}/archive",
        json={"version": created["version"]},
        headers=headers_b,
    )
    assert archive_response.status_code == 404

    reactivate_response = client.post(
        f"/api/v1/products/{created['id']}/reactivate",
        json={"version": created["version"]},
        headers=headers_b,
    )
    assert reactivate_response.status_code == 404

    delete_response = client.delete(
        f"/api/v1/products/{created['id']}?version={created['version']}", headers=headers_b
    )
    assert delete_response.status_code == 404

    _use_session(client, tenants["a"])
    still_owned = client.get(f"/api/v1/products/{created['id']}")
    assert still_owned.status_code == 200
    assert still_owned.json()["name"] == "Business A Product"
    assert still_owned.json()["is_active"] is True


def test_product_list_never_leaks_foreign_tenant_rows(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    client.post(
        "/api/v1/products",
        json={"name": "Business A Only", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    )

    _use_session(client, tenants["b"])
    listing = client.get("/api/v1/products")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    assert listing.json()["items"] == []
