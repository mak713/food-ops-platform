"""Phase 11 Settings tenant isolation (Spec AC-SEC-001..006; release-blocking).

The Settings resource has no id anywhere: the Business and User come only from the
authenticated session. These tests prove that a tenant can neither read another tenant's
Settings nor redirect a mutation to it by smuggling `business_id`, `owner_user_id`, `id`, or
other excluded fields into the body or query string.
"""

from __future__ import annotations

from sqlalchemy import text

from tests.integration.api.helpers import csrf_headers, signup

BASE = "/api/v1/settings"


def _signup_two(client):
    a = signup(client, email="set-a@example.com", name="Alice A", business_name="Bakery A")
    a_cookies = {"session": client.cookies.get("fo_session"), "csrf": client.cookies.get("fo_csrf")}
    client.cookies.clear()
    b = signup(client, email="set-b@example.com", name="Bob B", business_name="Bakery B")
    b_cookies = {"session": client.cookies.get("fo_session"), "csrf": client.cookies.get("fo_csrf")}
    return (a, a_cookies), (b, b_cookies)


def _use(client, tenant: dict) -> None:
    client.cookies.clear()
    client.cookies.set("fo_session", tenant["session"])
    client.cookies.set("fo_csrf", tenant["csrf"])


def _raw(client, path: str = BASE) -> bytes:
    response = client.get(path)
    assert response.status_code == 200
    return response.content


def test_each_tenant_reads_only_its_own_settings(client):
    (a, a_c), (b, b_c) = _signup_two(client)
    _use(client, a_c)
    body_a = client.get(BASE).json()
    _use(client, b_c)
    body_b = client.get(BASE).json()
    assert body_a["id"] == a["business"]["id"]
    assert body_b["id"] == b["business"]["id"]
    assert body_a["name"] == "Bakery A"
    assert body_b["name"] == "Bakery B"
    assert body_a["id"] != body_b["id"]


def test_a_cannot_mutate_b_even_with_every_redirect_attempt(client):
    (a, a_c), (b, b_c) = _signup_two(client)
    _use(client, b_c)
    b_before = _raw(client)
    b_owner_before = client.get("/api/v1/auth/me").content

    _use(client, a_c)
    response = client.patch(
        f"{BASE}/business?business_id={b['business']['id']}&owner_user_id={b['user']['id']}",
        json={
            "version": 1,
            "name": "Hijacked",
            "default_labor_rate": "99.00",
            "business_id": b["business"]["id"],
            "owner_user_id": b["user"]["id"],
            "id": b["business"]["id"],
            "default_fulfillment_method": "DELIVERY",
        },
        headers={**csrf_headers(client), "X-Business-Id": b["business"]["id"]},
    )
    assert response.status_code == 200
    assert response.json()["id"] == a["business"]["id"]
    assert response.json()["name"] == "Hijacked"  # A's own name changed — A is the actor

    owner = client.patch(
        f"{BASE}/owner?user_id={b['user']['id']}",
        json={
            "name": "Alice Renamed",
            "id": b["user"]["id"],
            "user_id": b["user"]["id"],
            "email": "b-takeover@example.com",
        },
        headers=csrf_headers(client),
    )
    assert owner.status_code == 200
    assert owner.json()["id"] == a["user"]["id"]

    _use(client, b_c)
    assert _raw(client) == b_before
    assert client.get("/api/v1/auth/me").content == b_owner_before


def test_responses_never_contain_the_other_tenants_identifiers_or_names(client):
    (a, a_c), (b, b_c) = _signup_two(client)
    _use(client, a_c)
    a_get = client.get(BASE).text
    a_patch = client.patch(
        f"{BASE}/business",
        json={"version": 1, "contact_phone": "111"},
        headers=csrf_headers(client),
    ).text
    a_owner = client.patch(
        f"{BASE}/owner", json={"name": "Alice Two"}, headers=csrf_headers(client)
    ).text
    forbidden = [b["business"]["id"], b["user"]["id"], "Bakery B", "Bob B", "set-b@example.com"]
    for body in (a_get, a_patch, a_owner):
        for token in forbidden:
            assert token not in body


def test_stale_version_conflict_is_scoped_to_the_callers_own_business(client):
    (a, a_c), (b, b_c) = _signup_two(client)
    _use(client, b_c)
    client.patch(
        f"{BASE}/business", json={"version": 1, "name": "B v2"}, headers=csrf_headers(client)
    )
    # A's version-1 write is not affected by B's version-2 state.
    _use(client, a_c)
    ok = client.patch(
        f"{BASE}/business", json={"version": 1, "name": "A v2"}, headers=csrf_headers(client)
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2


def test_csrf_and_session_must_belong_together(client):
    (a, a_c), (b, b_c) = _signup_two(client)
    # A's session cookie with B's CSRF cookie/header: the session-bound check rejects it.
    client.cookies.clear()
    client.cookies.set("fo_session", a_c["session"])
    client.cookies.set("fo_csrf", b_c["csrf"])
    response = client.patch(
        f"{BASE}/business",
        json={"version": 1, "name": "x"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 403
    _use(client, b_c)
    assert client.get(BASE).json()["name"] == "Bakery B"
    _use(client, a_c)
    assert client.get(BASE).json()["name"] == "Bakery A"


def test_owner_mutation_changes_exactly_one_user_row(client, session):
    (a, a_c), (b, b_c) = _signup_two(client)
    before = {
        r.id: (r.name, r.email, r.password_hash)
        for r in session.execute(text("select id::text, name, email, password_hash from users"))
    }
    _use(client, a_c)
    client.patch(f"{BASE}/owner", json={"name": "Alice Changed"}, headers=csrf_headers(client))
    session.expire_all()
    after = {
        r.id: (r.name, r.email, r.password_hash)
        for r in session.execute(text("select id::text, name, email, password_hash from users"))
    }
    changed = [uid for uid in after if after[uid] != before.get(uid)]
    assert changed == [a["user"]["id"]]
    assert after[b["user"]["id"]] == before[b["user"]["id"]]


def test_business_mutation_changes_exactly_one_business_row(client, session):
    (a, a_c), (b, b_c) = _signup_two(client)
    snapshot_sql = text(
        "select id::text, name, timezone, default_labor_rate::text, version, updated_at::text "
        "from businesses order by id"
    )
    before = {r.id: tuple(r)[1:] for r in session.execute(snapshot_sql)}
    _use(client, a_c)
    client.patch(
        f"{BASE}/business",
        json={"version": 1, "name": "A changed", "default_labor_rate": "7.00"},
        headers=csrf_headers(client),
    )
    session.expire_all()
    after = {r.id: tuple(r)[1:] for r in session.execute(snapshot_sql)}
    changed = [bid for bid in after if after[bid] != before[bid]]
    assert changed == [a["business"]["id"]]
