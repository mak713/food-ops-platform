"""Recipe identity CRUD/lifecycle — Phase 4 Plan v4 §5/§7/§14."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models.product import Product
from tests.integration.api.helpers import csrf_headers, signup


def _create_product(client, **overrides):
    signup(client)
    payload = {"name": "Sourdough Loaf", "product_type": "PRODUCED"}
    payload.update(overrides)
    return client.post("/api/v1/products", json=payload, headers=csrf_headers(client)).json()


def _create_ingredient(client, **overrides):
    payload = {"name": "Flour", "measurement_family": "WEIGHT", "canonical_unit": "g"}
    payload.update(overrides)
    return client.post("/api/v1/ingredients", json=payload, headers=csrf_headers(client)).json()


def _one_line(ingredient_id, **overrides):
    line = {"ingredient_id": ingredient_id, "quantity": "500", "unit": "g"}
    line.update(overrides)
    return line


def _recipe_payload(ingredient_id, **overrides):
    payload = {
        "name": "House Sourdough",
        "yield_quantity": "12",
        "active_time_minutes": 30,
        "ingredients": [_one_line(ingredient_id)],
    }
    payload.update(overrides)
    return payload


def test_create_recipe_on_produced_product_succeeds_atomically(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "House Sourdough"
    assert body["product_id"] == product["id"]
    revision = body["current_revision"]
    assert revision["revision_number"] == 1
    assert revision["is_current"] is True
    assert revision["yield_quantity"] == "12.000000"
    assert len(revision["ingredients"]) == 1
    assert revision["ingredients"][0]["ingredient_id"] == ingredient["id"]
    assert revision["ingredients"][0]["ingredient_name"] == "Flour"
    assert revision["ingredients"][0]["ingredient_is_active"] is True
    assert revision["ingredients"][0]["quantity"] == "500.000000"
    assert revision["ingredients"][0]["unit"] == "g"


def test_create_recipe_on_purchased_product_rejected(client):
    product = _create_product(client, product_type="PURCHASED")
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "RECIPE_REQUIRES_PRODUCED_PRODUCT"


def test_create_recipe_duplicate_rejected(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RECIPE_ALREADY_EXISTS"


def test_create_recipe_requires_at_least_one_ingredient_line(client):
    product = _create_product(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload("irrelevant", ingredients=[]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_create_recipe_rejects_cross_family_unit(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client, measurement_family="WEIGHT", canonical_unit="g")

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(
            ingredient["id"], ingredients=[_one_line(ingredient["id"], unit="mL")]
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "RECIPE_REVISION_INVALID_INGREDIENTS"
    assert body["error"]["issues"][0]["code"] == "CROSS_FAMILY_UNIT_CONVERSION"


def test_create_recipe_rejects_duplicate_ingredient_lines(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(
            ingredient["id"],
            ingredients=[_one_line(ingredient["id"]), _one_line(ingredient["id"], quantity="10")],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_create_recipe_rejects_archived_ingredient(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": ingredient["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "RECIPE_REVISION_INVALID_INGREDIENTS"
    assert body["error"]["issues"][0]["code"] == "INGREDIENT_NOT_ACTIVE"


def test_create_recipe_rejects_missing_ingredient_id(client):
    """The foreign-tenant-vs-missing identical-shape proof lives in
    test_tenant_isolation.py, alongside the established two-tenant helpers this needs."""
    product = _create_product(client)

    missing = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload("00000000-0000-0000-0000-000000000000"),
        headers=csrf_headers(client),
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["issues"][0]["code"] == "INGREDIENT_NOT_FOUND"


def test_create_recipe_rejects_malformed_ingredient_id_with_422_not_500(client):
    """Regression: `ingredient_id` is now a `uuid.UUID`-typed Pydantic field (not `str`),
    so a malformed value is rejected at request validation — never reaches service code
    that would previously call `uuid.UUID(...)` manually and raise an unhandled 500."""
    product = _create_product(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload("not-a-valid-uuid"),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text


def test_create_recipe_rejects_duplicate_ingredient_lines_using_different_uuid_representations(
    client,
):
    """Regression: two lines referencing the same logical Ingredient via different
    accepted textual UUID representations (differing only in case) must be rejected as
    duplicates — the duplicate check now compares normalized `uuid.UUID` objects (parsed
    by Pydantic), not raw textual strings that would previously compare unequal."""
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    lowercase_id = ingredient["id"]
    uppercase_id = ingredient["id"].upper()
    assert lowercase_id != uppercase_id  # sanity: genuinely different text

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(
            lowercase_id,
            ingredients=[
                _one_line(lowercase_id),
                _one_line(uppercase_id, quantity="10"),
            ],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text


def test_create_recipe_ingredient_validation_failure_releases_the_product_lock(client, session):
    """Regression (remediation item 4): `create_recipe` holds the Product row's `FOR
    UPDATE` lock via `get_product_for_business_locked`, then calls
    `_validate_and_lock_ingredient_lines`, which also locks the submitted Ingredient
    rows. When that call raises its expected `RECIPE_REVISION_INVALID_INGREDIENTS`
    `ApiError`, the transaction holding both locks must be explicitly rolled back before
    the error propagates — not left open. Proven here by checking the shared `session`
    (the same object the request ran against, via the `client` fixture's dependency
    override) is no longer mid-transaction and remains fully usable for a fresh query
    immediately afterward."""
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": ingredient["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "RECIPE_REVISION_INVALID_INGREDIENTS"

    # The lock-owning transaction must have been rolled back, not left open.
    assert session.in_transaction() is False
    # And the session itself must remain fully usable — a fresh query succeeds cleanly,
    # proving no lingering broken/aborted transaction state.
    assert session.scalar(select(Product).where(Product.id == product["id"])) is not None


def test_get_recipe_returns_404_when_none_exists_yet(client):
    product = _create_product(client)
    response = client.get(f"/api/v1/products/{product['id']}/recipe")
    assert response.status_code == 404


def test_get_recipe_returns_current_revision(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )

    response = client.get(f"/api/v1/products/{product['id']}/recipe")
    assert response.status_code == 200
    assert response.json()["current_revision"]["revision_number"] == 1


def test_rename_recipe_does_not_touch_revision_content(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    )

    response = client.patch(
        f"/api/v1/products/{product['id']}/recipe",
        json={"name": "Renamed Recipe"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Recipe"
    assert body["current_revision"]["revision_number"] == 1
    assert body["current_revision"]["yield_quantity"] == "12.000000"
