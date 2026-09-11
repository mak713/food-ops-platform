"""Selling Option nested CRUD/archive/reactivate/delete semantics — Phase 3 plan v3 §13."""

from __future__ import annotations

from sqlalchemy import select

from app.db.enums import OrderLineType
from app.db.models.product import Product, SellingOption
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import make_order, make_order_line


def _create_product(client, **overrides):
    signup(client)
    payload = {"name": "Sourdough Loaf", "product_type": "PRODUCED"}
    payload.update(overrides)
    return client.post("/api/v1/products", json=payload, headers=csrf_headers(client)).json()


def _create_option(client, product_id, **overrides):
    payload = {"name": "6-pack", "quantity_units": 6, "price": "12.00"}
    payload.update(overrides)
    return client.post(
        f"/api/v1/products/{product_id}/selling-options",
        json=payload,
        headers=csrf_headers(client),
    )


def test_create_selling_option_on_freshly_created_empty_product(client):
    """The normal path now, not an edge case (Phase 3 plan v3 §9): a brand-new Product
    has zero Selling Options until the owner adds some here."""
    product = _create_product(client)
    assert product["selling_options"] == []

    response = _create_option(client, product["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["product_id"] == product["id"]
    # quantity_units is NUMERIC(18,6); price is NUMERIC(14,2) — each serializes at its
    # column's declared precision (Spec §19.20 Decimal-safe, not float-approximate).
    assert body["quantity_units"] == "6.000000"
    assert body["price"] == "12.00"
    assert body["is_active"] is True
    assert body["version"] == 1

    refreshed = client.get(f"/api/v1/products/{product['id']}")
    assert len(refreshed.json()["selling_options"]) == 1


def test_create_selling_option_invalid_quantity_or_price_rejected(client):
    product = _create_product(client)
    zero_qty = _create_option(client, product["id"], quantity_units=0)
    assert zero_qty.status_code == 422
    negative_price = _create_option(client, product["id"], price=-1)
    assert negative_price.status_code == 422


def test_update_selling_option(client):
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    response = client.patch(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}",
        json={"version": option["version"], "price": "15.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    assert response.json()["price"] == "15.00"
    assert response.json()["version"] == 2


def test_update_selling_option_explicit_null_on_non_nullable_fields_rejected(client):
    """PATCH omitted-vs-null validation (Checkpoint 3 remediation) — every mutable
    SellingOption field is NOT NULL, so explicit null must be rejected for all of them."""
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    for field, value in [
        ("name", None),
        ("quantity_units", None),
        ("price", None),
        ("packaging_cost", None),
        ("sort_order", None),
    ]:
        response = client.patch(
            f"/api/v1/products/{product['id']}/selling-options/{option['id']}",
            json={"version": option["version"], field: value},
            headers=csrf_headers(client),
        )
        assert response.status_code == 422, field


def test_stale_version_on_selling_option_update_returns_409(client):
    """Product/SellingOption stale-version mutation coverage (Checkpoint 3 remediation)."""
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    response = client.patch(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}",
        json={"version": option["version"] + 1, "price": "1.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "STALE_VERSION"
    assert "changed since you opened it" in body["error"]["message"]


def test_stale_version_on_selling_option_archive_returns_409(client):
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    response = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/archive",
        json={"version": option["version"] + 1},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"


def test_delete_selling_option_stale_version_is_distinct_from_reference_conflict(client):
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    response = client.delete(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}"
        f"?version={option['version'] + 1}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"


def test_selling_option_from_different_product_returns_404(client):
    """Rewritten (Checkpoint 3 remediation) to prove cross-product isolation entirely
    through the approved nested API surface (PATCH/archive/delete/list), since the
    unapproved single-resource GET has been removed."""
    signup(client)
    product_a = client.post(
        "/api/v1/products",
        json={"name": "Product A", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    product_b = client.post(
        "/api/v1/products",
        json={"name": "Product B", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    option_of_b = _create_option(client, product_b["id"]).json()

    patch_response = client.patch(
        f"/api/v1/products/{product_a['id']}/selling-options/{option_of_b['id']}",
        json={"version": option_of_b["version"], "price": "1.00"},
        headers=csrf_headers(client),
    )
    assert patch_response.status_code == 404

    archive_response = client.post(
        f"/api/v1/products/{product_a['id']}/selling-options/{option_of_b['id']}/archive",
        json={"version": option_of_b["version"]},
        headers=csrf_headers(client),
    )
    assert archive_response.status_code == 404

    delete_response = client.delete(
        f"/api/v1/products/{product_a['id']}/selling-options/{option_of_b['id']}"
        f"?version={option_of_b['version']}",
        headers=csrf_headers(client),
    )
    assert delete_response.status_code == 404

    # Confirms none of the above mutated it — still present, unchanged, under its real
    # product's list.
    listing = client.get(f"/api/v1/products/{product_b['id']}/selling-options")
    assert listing.status_code == 200
    assert [o["id"] for o in listing.json()] == [option_of_b["id"]]


def test_archive_and_reactivate_selling_option_is_idempotent(client):
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    archived = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/archive",
        json={"version": option["version"]},
        headers=csrf_headers(client),
    )
    assert archived.status_code == 200
    assert archived.json()["is_active"] is False
    assert archived.json()["version"] == 2

    archived_again = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/archive",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert archived_again.status_code == 200
    assert archived_again.json()["is_active"] is False
    assert archived_again.json()["version"] == 2

    reactivated = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/reactivate",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    assert reactivated.json()["version"] == 3

    reactivated_again = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/reactivate",
        json={"version": 3},
        headers=csrf_headers(client),
    )
    assert reactivated_again.status_code == 200
    assert reactivated_again.json()["is_active"] is True
    assert reactivated_again.json()["version"] == 3


def test_selling_option_mutations_work_when_parent_product_is_archived(client):
    """Phase 3 plan v3 §7: editability is independent of the parent's archived state."""
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    created = _create_option(client, product["id"])
    assert created.status_code == 201, created.text
    option = created.json()

    updated = client.patch(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}",
        json={"version": option["version"], "price": "20.00"},
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200

    archived_option = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/archive",
        json={"version": updated.json()["version"]},
        headers=csrf_headers(client),
    )
    assert archived_option.status_code == 200


def test_archiving_product_does_not_change_selling_option_is_active(client):
    """Phase 3 plan v3 §6: no cascade from Product.archive to SellingOption.is_active."""
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()
    assert option["is_active"] is True

    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    # The single-resource GET was removed (Checkpoint 3 remediation) — read the option's
    # current state via the approved list endpoint instead.
    listing = client.get(f"/api/v1/products/{product['id']}/selling-options")
    assert listing.status_code == 200
    unchanged = next(o for o in listing.json() if o["id"] == option["id"])
    assert unchanged["is_active"] is True
    assert unchanged["version"] == option["version"]


def test_delete_unused_selling_option_succeeds(client):
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    response = client.delete(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}"
        f"?version={option['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 204


def test_delete_selling_option_referenced_by_order_line_is_blocked(client, session):
    product = _create_product(client)
    option = _create_option(client, product["id"]).json()

    product_row = session.execute(select(Product).where(Product.id == product["id"])).scalar_one()
    selling_option_row = next(o for o in product_row.selling_options if str(o.id) == option["id"])
    order = make_order(session, product_row.business)
    make_order_line(
        session,
        product_row.business,
        order,
        line_type=OrderLineType.STANDARD_OPTION,
        product=product_row,
        selling_option=selling_option_row,
    )
    session.commit()

    response = client.delete(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}"
        f"?version={option['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELLING_OPTION_HAS_REFERENCES"


def test_deleting_unreferenced_product_cascades_its_selling_options(client, session):
    product = _create_product(client)
    _create_option(client, product["id"])
    _create_option(client, product["id"], name="12-pack", quantity_units=12)

    response = client.delete(
        f"/api/v1/products/{product['id']}?version={product['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 204

    remaining = (
        session.execute(select(SellingOption).where(SellingOption.product_id == product["id"]))
        .scalars()
        .all()
    )
    assert remaining == []


def test_selling_option_ordering_updates_after_sort_order_change(client):
    """Manual-testing remediation: ordering must reflect a mutation, not just the order
    options were originally created in. `test_deterministic_selling_option_ordering` (in
    test_products.py) only proves initial-fetch ordering for options created out of order;
    this proves that changing an *existing* option's `sort_order` via PATCH actually moves
    it, on both the detail-embedded list and the standalone list endpoint."""
    product = _create_product(client)
    first = _create_option(client, product["id"], name="First", sort_order=10).json()
    second = _create_option(client, product["id"], name="Second", sort_order=20).json()

    detail_before = client.get(f"/api/v1/products/{product['id']}")
    assert [o["name"] for o in detail_before.json()["selling_options"]] == ["First", "Second"]

    # Move "First" past "Second" by giving it a higher sort_order than "Second"'s.
    moved = client.patch(
        f"/api/v1/products/{product['id']}/selling-options/{first['id']}",
        json={"version": first["version"], "sort_order": 25},
        headers=csrf_headers(client),
    )
    assert moved.status_code == 200

    detail_after = client.get(f"/api/v1/products/{product['id']}")
    assert [o["name"] for o in detail_after.json()["selling_options"]] == ["Second", "First"]

    listing_after = client.get(f"/api/v1/products/{product['id']}/selling-options")
    assert [o["name"] for o in listing_after.json()] == ["Second", "First"]

    # Sanity: "Second" was never touched.
    assert second["sort_order"] == 20


def test_archive_and_reactivate_do_not_change_selling_option_ordering(client):
    """Manual-testing remediation: archiving/reactivating a selling option must not move
    it — a lifecycle action is not a reorder, and neither should have the side effect of
    bumping the mutated row to the end of the list (the reported symptom)."""
    product = _create_product(client)
    first = _create_option(client, product["id"], name="First", sort_order=10).json()
    second = _create_option(client, product["id"], name="Second", sort_order=20).json()
    third = _create_option(client, product["id"], name="Third", sort_order=30).json()

    archived = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{second['id']}/archive",
        json={"version": second["version"]},
        headers=csrf_headers(client),
    )
    assert archived.status_code == 200

    after_archive = client.get(f"/api/v1/products/{product['id']}")
    assert [o["name"] for o in after_archive.json()["selling_options"]] == [
        "First",
        "Second",
        "Third",
    ]

    reactivated = client.post(
        f"/api/v1/products/{product['id']}/selling-options/{second['id']}/reactivate",
        json={"version": archived.json()["version"]},
        headers=csrf_headers(client),
    )
    assert reactivated.status_code == 200

    after_reactivate = client.get(f"/api/v1/products/{product['id']}")
    assert [o["name"] for o in after_reactivate.json()["selling_options"]] == [
        "First",
        "Second",
        "Third",
    ]

    assert first["id"] and third["id"]  # both untouched, present throughout
