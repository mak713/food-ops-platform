"""Product CRUD/list/archive/reactivate/delete/product-type rules — Phase 3 plan v3 §13."""

from __future__ import annotations

from sqlalchemy import event, select

from app.db.enums import OrderLineType
from app.db.models.product import Product
from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup
from tests.integration.schema.factories import make_order, make_order_line, make_recipe


def _create_product(client, **overrides):
    signup(client)
    payload = {"name": "Sourdough Loaf", "product_type": "PRODUCED"}
    payload.update(overrides)
    return client.post("/api/v1/products", json=payload, headers=csrf_headers(client))


def test_create_produced_product(client):
    response = _create_product(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["product_type"] == "PRODUCED"
    assert body["is_active"] is True
    assert body["selling_options"] == []
    assert body["version"] == 1


def test_create_purchased_product(client):
    response = _create_product(client, product_type="PURCHASED", name="Canned Jam")
    assert response.status_code == 201, response.text
    assert response.json()["product_type"] == "PURCHASED"


def test_create_product_no_nested_selling_options_field(client):
    """Phase 3 plan v3 §9: a `selling_options` field in the request body is simply
    ignored/rejected by the schema — there is no nested-creation path at all."""
    signup(client)
    response = client.post(
        "/api/v1/products",
        json={
            "name": "Test",
            "product_type": "PRODUCED",
            "selling_options": [{"name": "x", "quantity_units": 1, "price": 1}],
        },
        headers=csrf_headers(client),
    )
    # Pydantic ignores unknown fields by default; the product is still created with no
    # selling options, proving the extra field had no nested-creation effect.
    assert response.status_code == 201
    assert response.json()["selling_options"] == []


def test_create_product_surplus_field_combinations(client):
    signup(client)
    combos = [
        {"can_reuse_surplus": False, "default_surplus_usable_days": None},
        {"can_reuse_surplus": False, "default_surplus_usable_days": 5},
        {"can_reuse_surplus": True, "default_surplus_usable_days": None},
        {"can_reuse_surplus": True, "default_surplus_usable_days": 5},
    ]
    for combo in combos:
        payload = {"name": "Combo Product", "product_type": "PRODUCED", **combo}
        response = client.post("/api/v1/products", json=payload, headers=csrf_headers(client))
        assert response.status_code == 201, (combo, response.text)
        body = response.json()
        assert body["can_reuse_surplus"] == combo["can_reuse_surplus"]
        assert body["default_surplus_usable_days"] == combo["default_surplus_usable_days"]


def test_create_product_invalid_surplus_usable_days_rejected(client):
    signup(client)
    response = client.post(
        "/api/v1/products",
        json={
            "name": "Bad Product",
            "product_type": "PRODUCED",
            "default_surplus_usable_days": 0,
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_product_type_is_immutable(client):
    created = _create_product(client).json()
    response = client.patch(
        f"/api/v1/products/{created['id']}",
        json={"version": created["version"], "product_type": "PURCHASED", "name": "Renamed"},
        headers=csrf_headers(client),
    )
    # Pydantic ignores the unknown `product_type` field on ProductUpdateRequest — only
    # `name` is actually applied.
    assert response.status_code == 200
    body = response.json()
    assert body["product_type"] == "PRODUCED"
    assert body["name"] == "Renamed"


def test_list_and_get_products(client):
    signup(client)
    for name, ptype in [("Bagel", "PRODUCED"), ("Jam", "PURCHASED")]:
        client.post(
            "/api/v1/products",
            json={"name": name, "product_type": ptype},
            headers=csrf_headers(client),
        )

    listing = client.get("/api/v1/products")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    names = [p["name"] for p in listing.json()["items"]]
    assert names == sorted(names)

    product_id = listing.json()["items"][0]["id"]
    detail = client.get(f"/api/v1/products/{product_id}")
    assert detail.status_code == 200


def test_list_products_has_active_selling_option_flag(client):
    """Manual Acceptance Pricing/UX Correction §1 — the list endpoint's
    `has_active_selling_option` flag lets the Order Entry Standard Option picker exclude
    a Product with zero active Selling Options from *new* selection without an N+1 fetch
    per Product. Covers all three cases: no Selling Options at all, only an archived one,
    and at least one active one."""
    signup(client)
    client.post(
        "/api/v1/products",
        json={"name": "No Options", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    )
    only_archived = client.post(
        "/api/v1/products",
        json={"name": "Only Archived", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    with_active = client.post(
        "/api/v1/products",
        json={"name": "With Active", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    archived_option = client.post(
        f"/api/v1/products/{only_archived['id']}/selling-options",
        json={"name": "Old Option", "quantity_units": 1, "price": 1},
        headers=csrf_headers(client),
    ).json()
    client.post(
        f"/api/v1/products/{only_archived['id']}/selling-options/{archived_option['id']}/archive",
        json={"version": archived_option["version"]},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/products/{with_active['id']}/selling-options",
        json={"name": "Six Pack", "quantity_units": 6, "price": 12},
        headers=csrf_headers(client),
    )

    listing = client.get("/api/v1/products").json()
    flags_by_name = {p["name"]: p["has_active_selling_option"] for p in listing["items"]}
    assert flags_by_name["No Options"] is False
    assert flags_by_name["Only Archived"] is False
    assert flags_by_name["With Active"] is True


def test_list_products_eligibility_tenant_isolated_paginated_and_no_n_plus_one(client, session):
    """STANDARD_OPTION End-to-End Fix §9 — a second, real-database integration test for
    `has_active_selling_option` (not a replacement for the one above) that additionally
    proves: the flag and the listing itself stay correctly tenant-scoped; pagination
    still works against the tuple-returning `list_products_for_business`; and computing
    the flag for every Product in one list call issues the same number of SQL statements
    regardless of how many Products exist — never one extra query per Product (no N+1).
    """
    tenant_a_email = "tenant-a-eligibility@example.com"
    tenant_a_password = "tenant a correct horse battery"
    signup(client, email=tenant_a_email, password=tenant_a_password)

    product_a = client.post(
        "/api/v1/products",
        json={"name": "Eligible A", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    client.post(
        f"/api/v1/products/{product_a['id']}/selling-options",
        json={"name": "Option", "quantity_units": 1, "price": 5},
        headers=csrf_headers(client),
    )
    client.post(
        "/api/v1/products",
        json={"name": "No Options B", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    )
    product_c = client.post(
        "/api/v1/products",
        json={"name": "Only Inactive C", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    inactive_option = client.post(
        f"/api/v1/products/{product_c['id']}/selling-options",
        json={"name": "Old", "quantity_units": 1, "price": 1},
        headers=csrf_headers(client),
    ).json()
    client.post(
        f"/api/v1/products/{product_c['id']}/selling-options/{inactive_option['id']}/archive",
        json={"version": inactive_option["version"]},
        headers=csrf_headers(client),
    )

    # Pagination still works against the (Product, bool) tuple-returning list function:
    # two pages together cover exactly the three Products created above, no overlap/gap.
    page1 = client.get("/api/v1/products?limit=2&offset=0").json()
    page2 = client.get("/api/v1/products?limit=2&offset=2").json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    names_seen = {p["name"] for p in page1["items"]} | {p["name"] for p in page2["items"]}
    assert names_seen == {"Eligible A", "No Options B", "Only Inactive C"}

    def _count_sql_statements(url: str) -> tuple[int, dict]:
        count = 0

        def _tick(*_args, **_kwargs):
            nonlocal count
            count += 1

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", _tick)
        try:
            body = client.get(url).json()
        finally:
            event.remove(connection, "before_cursor_execute", _tick)
        return count, body

    # Baseline: one list call against 3 Products.
    count_with_3, listing_with_3 = _count_sql_statements("/api/v1/products?limit=200")
    assert listing_with_3["total"] == 3
    flags_by_name = {p["name"]: p["has_active_selling_option"] for p in listing_with_3["items"]}
    assert flags_by_name["Eligible A"] is True
    assert flags_by_name["No Options B"] is False
    assert flags_by_name["Only Inactive C"] is False

    # Add 3 more (eligible) Products and issue the identical list call again — an N+1
    # implementation would issue 3 additional per-Product queries here; the actual
    # correlated-EXISTS implementation issues exactly the same number of statements
    # regardless of how many Products are being listed.
    for name in ("Eligible D", "Eligible E", "Eligible F"):
        extra = client.post(
            "/api/v1/products",
            json={"name": name, "product_type": "PRODUCED"},
            headers=csrf_headers(client),
        ).json()
        client.post(
            f"/api/v1/products/{extra['id']}/selling-options",
            json={"name": "Option", "quantity_units": 1, "price": 5},
            headers=csrf_headers(client),
        )
    count_with_6, listing_with_6 = _count_sql_statements("/api/v1/products?limit=200")
    assert listing_with_6["total"] == 6
    assert count_with_6 == count_with_3, (
        "SQL statement count for the Product list scaled with the number of Products "
        f"({count_with_3} -> {count_with_6}) — this is the N+1 signature the "
        "correlated-EXISTS design is supposed to avoid."
    )

    # Tenant isolation: a second business's identically-named, independently-eligible
    # Product must never appear in tenant A's own listing, and tenant B's own listing
    # must never see any of tenant A's Products either.
    signup(
        client, email="tenant-b-eligibility@example.com", password="tenant b correct horse battery"
    )
    client.post(
        "/api/v1/products",
        json={"name": "Eligible A", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    )
    tenant_b_listing = client.get("/api/v1/products").json()
    assert tenant_b_listing["total"] == 1
    assert tenant_b_listing["items"][0]["has_active_selling_option"] is False

    client.post(
        "/api/v1/auth/login",
        json={"email": tenant_a_email, "password": tenant_a_password},
        headers=ORIGIN_HEADERS,
    )
    tenant_a_listing_again = client.get("/api/v1/products").json()
    assert tenant_a_listing_again["total"] == 6
    assert {p["name"] for p in tenant_a_listing_again["items"]} == {
        "Eligible A",
        "No Options B",
        "Only Inactive C",
        "Eligible D",
        "Eligible E",
        "Eligible F",
    }


def test_archive_and_reactivate_product_is_idempotent(client):
    created = _create_product(client).json()
    product_id = created["id"]

    archived = client.post(
        f"/api/v1/products/{product_id}/archive",
        json={"version": created["version"]},
        headers=csrf_headers(client),
    )
    assert archived.status_code == 200
    assert archived.json()["version"] == 2

    archived_again = client.post(
        f"/api/v1/products/{product_id}/archive",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert archived_again.json()["version"] == 2

    reactivated = client.post(
        f"/api/v1/products/{product_id}/reactivate",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["version"] == 3


def test_delete_product_with_no_references_succeeds(client):
    created = _create_product(client).json()
    response = client.delete(
        f"/api/v1/products/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 204


def test_delete_product_with_recipe_is_blocked(client, session):
    created = _create_product(client).json()

    product_row = session.execute(select(Product).where(Product.id == created["id"])).scalar_one()
    make_recipe(session, product_row.business, product_row)
    session.commit()

    response = client.delete(
        f"/api/v1/products/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_HAS_RECIPE"


def test_delete_product_referenced_by_order_line_is_blocked(client, session):
    created = _create_product(client).json()

    product_row = session.execute(select(Product).where(Product.id == created["id"])).scalar_one()
    order = make_order(session, product_row.business)
    make_order_line(
        session,
        product_row.business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product_row,
    )
    session.commit()

    response = client.delete(
        f"/api/v1/products/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRODUCT_HAS_REFERENCES"


def test_stale_version_on_product_update_returns_409(client):
    """Product/SellingOption stale-version mutation coverage (Checkpoint 3 remediation) —
    mirrors Customer's equivalent test."""
    created = _create_product(client).json()
    response = client.patch(
        f"/api/v1/products/{created['id']}",
        json={"version": created["version"] + 1, "name": "New Name"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "STALE_VERSION"
    assert "changed since you opened it" in body["error"]["message"]


def test_stale_version_on_product_archive_returns_409(client):
    created = _create_product(client).json()
    response = client.post(
        f"/api/v1/products/{created['id']}/archive",
        json={"version": created["version"] + 1},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"


def test_delete_product_stale_version_is_distinct_from_reference_conflict(client):
    """DELETE must report STALE_VERSION for a wrong version, not a reference conflict,
    when there is no actual reference (mirrors the equivalent Customer test)."""
    created = _create_product(client).json()
    response = client.delete(
        f"/api/v1/products/{created['id']}?version={created['version'] + 1}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"


def test_update_product_explicit_null_on_non_nullable_fields_rejected(client):
    """PATCH omitted-vs-null validation (Checkpoint 3 remediation): omitting a field
    means "leave unchanged"; explicitly sending it as null for a NOT NULL domain field
    must be a normal 422, never reach the database."""
    created = _create_product(client).json()
    for field, value in [
        ("name", None),
        ("default_packaging_cost", None),
        ("can_reuse_surplus", None),
    ]:
        response = client.patch(
            f"/api/v1/products/{created['id']}",
            json={"version": created["version"], field: value},
            headers=csrf_headers(client),
        )
        assert response.status_code == 422, field


def test_update_product_explicit_null_on_nullable_fields_clears_them(client):
    created = _create_product(client, description="Something", default_surplus_usable_days=5).json()
    response = client.patch(
        f"/api/v1/products/{created['id']}",
        json={
            "version": created["version"],
            "description": None,
            "default_surplus_usable_days": None,
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] is None
    assert body["default_surplus_usable_days"] is None


def test_deterministic_selling_option_ordering(client, session):
    """SellingOption output ordering must be explicit by sort_order with a deterministic
    tiebreaker (Checkpoint 3 remediation) — inserted out of order here, must come back
    sorted."""
    product = _create_product(client).json()
    for name, sort_order in [("Third", 30), ("First", 10), ("Second", 20)]:
        client.post(
            f"/api/v1/products/{product['id']}/selling-options",
            json={"name": name, "quantity_units": 1, "price": "1.00", "sort_order": sort_order},
            headers=csrf_headers(client),
        )

    detail = client.get(f"/api/v1/products/{product['id']}")
    names = [o["name"] for o in detail.json()["selling_options"]]
    assert names == ["First", "Second", "Third"]

    listing = client.get(f"/api/v1/products/{product['id']}/selling-options")
    assert [o["name"] for o in listing.json()] == ["First", "Second", "Third"]
