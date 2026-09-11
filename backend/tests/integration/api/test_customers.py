"""Customer CRUD/list/duplicate-warning/archive/reactivate/delete — Phase 3 plan v3 §12."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models.customer import Customer
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import make_order


def _create_customer(client, **overrides):
    signup(client)
    payload = {"name": "Ada Lovelace"}
    payload.update(overrides)
    return client.post("/api/v1/customers", json=payload, headers=csrf_headers(client))


def test_create_customer_with_only_name(client):
    response = _create_customer(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Ada Lovelace"
    assert body["phone"] is None
    assert body["email"] is None
    assert body["is_active"] is True
    assert body["version"] == 1


def test_create_customer_with_all_fields(client):
    response = _create_customer(
        client,
        phone=" 555-0100 ",
        email=" Ada@Example.com ",
        preferred_contact_method="EMAIL",
        notes="VIP customer",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # Phone: trimmed only, no further normalization (Phase 3 plan v3 §11/§12).
    assert body["phone"] == "555-0100"
    # Email: trimmed + case-folded.
    assert body["email"] == "ada@example.com"
    assert body["notes"] == "VIP customer"


def test_create_customer_blank_name_rejected(client):
    signup(client)
    response = client.post("/api/v1/customers", json={"name": "   "}, headers=csrf_headers(client))
    assert response.status_code == 422


def test_create_customer_duplicate_name_warns_then_confirm_succeeds(client):
    signup(client)
    first = client.post(
        "/api/v1/customers", json={"name": "Grace Hopper"}, headers=csrf_headers(client)
    )
    assert first.status_code == 201

    warned = client.post(
        "/api/v1/customers", json={"name": "grace hopper"}, headers=csrf_headers(client)
    )
    assert warned.status_code == 422
    body = warned.json()
    assert body["error"]["issues"][0]["code"] == "POSSIBLE_DUPLICATE_CUSTOMER"
    assert body["error"]["issues"][0]["severity"] == "WARNING"

    confirmed = client.post(
        "/api/v1/customers",
        json={"name": "grace hopper", "confirm_duplicate": True},
        headers=csrf_headers(client),
    )
    assert confirmed.status_code == 201


def test_duplicate_check_matches_on_phone_and_email(client):
    signup(client)
    client.post(
        "/api/v1/customers",
        json={"name": "First Name", "phone": "555-0100", "email": "shared@example.com"},
        headers=csrf_headers(client),
    )

    by_phone = client.post(
        "/api/v1/customers",
        json={"name": "Totally Different Name", "phone": "555-0100"},
        headers=csrf_headers(client),
    )
    assert by_phone.status_code == 422

    by_email = client.post(
        "/api/v1/customers",
        json={"name": "Another Different Name", "email": "SHARED@example.com"},
        headers=csrf_headers(client),
    )
    assert by_email.status_code == 422


def test_list_customers_search_and_pagination(client):
    signup(client)
    for name in ["Alpha Bakery Customer", "Beta Bakery Customer", "Gamma Bakery Customer"]:
        client.post("/api/v1/customers", json={"name": name}, headers=csrf_headers(client))

    response = client.get("/api/v1/customers?q=Beta")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Beta Bakery Customer"

    paged = client.get("/api/v1/customers?limit=2&offset=0")
    assert paged.status_code == 200
    assert paged.json()["total"] == 3
    assert len(paged.json()["items"]) == 2
    # Default sort: name ascending (Phase 3 plan v3 §11).
    names = [c["name"] for c in paged.json()["items"]]
    assert names == sorted(names)


def test_list_customers_is_active_filter(client):
    created = _create_customer(client).json()
    client.post(
        f"/api/v1/customers/{created['id']}/archive",
        json={"version": created["version"]},
        headers=csrf_headers(client),
    )

    active_only = client.get("/api/v1/customers?is_active=true")
    assert active_only.json()["total"] == 0

    archived_only = client.get("/api/v1/customers?is_active=false")
    assert archived_only.json()["total"] == 1


def test_get_customer(client):
    created = _create_customer(client).json()
    response = client.get(f"/api/v1/customers/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_update_customer_partial_fields(client):
    created = _create_customer(client).json()
    response = client.patch(
        f"/api/v1/customers/{created['id']}",
        json={"version": created["version"], "phone": "555-9999"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == "555-9999"
    assert body["name"] == "Ada Lovelace"
    assert body["version"] == 2


def test_update_customer_unrelated_field_does_not_retrigger_duplicate_check(client):
    signup(client)
    a = client.post(
        "/api/v1/customers", json={"name": "Same Name"}, headers=csrf_headers(client)
    ).json()
    b = client.post(
        "/api/v1/customers",
        json={"name": "Same Name", "confirm_duplicate": True},
        headers=csrf_headers(client),
    ).json()

    response = client.patch(
        f"/api/v1/customers/{b['id']}",
        json={"version": b["version"], "phone": "555-1111"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    assert a["id"] != b["id"]


def test_update_customer_explicit_null_name_rejected(client):
    """PATCH omitted-vs-null validation (Checkpoint 3 remediation): omitting `name` means
    "leave unchanged"; explicitly sending it as null must be a normal 422, never reach
    the database (`customers.name` is NOT NULL)."""
    created = _create_customer(client).json()
    response = client.patch(
        f"/api/v1/customers/{created['id']}",
        json={"version": created["version"], "name": None},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_update_customer_explicit_null_on_nullable_fields_clears_them(client):
    created = _create_customer(
        client, phone="555-0100", email="ada@example.com", notes="VIP"
    ).json()
    response = client.patch(
        f"/api/v1/customers/{created['id']}",
        json={
            "version": created["version"],
            "phone": None,
            "email": None,
            "preferred_contact_method": None,
            "notes": None,
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] is None
    assert body["email"] is None
    assert body["preferred_contact_method"] is None
    assert body["notes"] is None


def test_archive_and_reactivate_are_idempotent(client):
    created = _create_customer(client).json()
    customer_id = created["id"]

    archived = client.post(
        f"/api/v1/customers/{customer_id}/archive",
        json={"version": created["version"]},
        headers=csrf_headers(client),
    )
    assert archived.status_code == 200
    assert archived.json()["is_active"] is False
    assert archived.json()["version"] == 2

    # Idempotent re-archive: no version bump, no error.
    archived_again = client.post(
        f"/api/v1/customers/{customer_id}/archive",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert archived_again.status_code == 200
    assert archived_again.json()["version"] == 2

    reactivated = client.post(
        f"/api/v1/customers/{customer_id}/reactivate",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    assert reactivated.json()["version"] == 3

    reactivated_again = client.post(
        f"/api/v1/customers/{customer_id}/reactivate",
        json={"version": 3},
        headers=csrf_headers(client),
    )
    assert reactivated_again.status_code == 200
    assert reactivated_again.json()["version"] == 3


def test_stale_version_on_update_returns_409(client):
    created = _create_customer(client).json()
    response = client.patch(
        f"/api/v1/customers/{created['id']}",
        json={"version": created["version"] + 1, "phone": "555-0000"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "STALE_VERSION"
    assert "changed since you opened it" in body["error"]["message"]


def test_delete_customer_with_no_history_succeeds(client):
    created = _create_customer(client).json()
    response = client.delete(
        f"/api/v1/customers/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 204

    follow_up = client.get(f"/api/v1/customers/{created['id']}")
    assert follow_up.status_code == 404


def test_delete_customer_with_order_history_is_blocked(client, session):
    created = _create_customer(client).json()

    business_row = session.execute(
        select(Customer).where(Customer.id == created["id"])
    ).scalar_one()
    make_order(session, business_row.business, customer_id=business_row.id)
    session.commit()

    response = client.delete(
        f"/api/v1/customers/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CUSTOMER_HAS_ORDER_HISTORY"


def test_delete_customer_stale_version_is_distinct_from_reference_conflict(client):
    """DELETE must report STALE_VERSION for a wrong version, not a reference conflict,
    when there is no actual reference (Phase 3 plan v3 §18)."""
    created = _create_customer(client).json()
    response = client.delete(
        f"/api/v1/customers/{created['id']}?version={created['version'] + 1}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"
