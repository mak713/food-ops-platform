"""Purchased Product Inventory API tests (Phase 5 Plan §G): Initial Balance, first-ever
Restock, ordinary Restock, Manual Adjustment, replacement-cost maintenance, history, and
the top-level Inventory-module list endpoint."""

from __future__ import annotations

from tests.integration.api.helpers import csrf_headers, signup


def _create_product(client, **overrides) -> dict:
    payload = {"name": "Canned Tomatoes", "product_type": "PURCHASED"}
    payload.update(overrides)
    return client.post("/api/v1/products", json=payload, headers=csrf_headers(client)).json()


# --- Purchased-only type enforcement ---------------------------------------------------


def test_initial_balance_rejects_a_produced_product(client):
    signup(client)
    product = _create_product(client, product_type="PRODUCED")

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_REQUIRES_PURCHASED_PRODUCT"


def test_get_purchased_inventory_404s_before_any_initialization(client):
    signup(client)
    product = _create_product(client)

    response = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_NOT_INITIALIZED"


# --- Initial Balance -------------------------------------------------------------------


def test_initial_balance_creates_the_row_with_no_purchase_evidence(client):
    signup(client)
    product = _create_product(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "12.5", "unit_cost": "3.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["physical_quantity"] == "12.500000"
    assert body["weighted_average_unit_cost"] == "3.000000"
    assert body["latest_purchase_unit_cost"] is None
    assert body["replacement_unit_cost"] is None
    assert body["version"] == 1

    txns = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory/transactions",
        headers=csrf_headers(client),
    ).json()
    assert txns["total"] == 1
    assert txns["items"][0]["transaction_type"] == "INITIAL_BALANCE"


def test_initial_balance_rejects_a_second_attempt(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "5", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_ALREADY_INITIALIZED"


def test_initial_balance_rejects_an_inactive_product(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_PRODUCT_INACTIVE"


# --- First-ever Restock (may itself create the row) -------------------------------------


def test_first_restock_creates_the_row_and_sets_latest_purchase_cost(client):
    signup(client)
    product = _create_product(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"quantity": "20", "unit_cost": "4.50"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["physical_quantity"] == "20.000000"
    assert body["weighted_average_unit_cost"] == "4.500000"
    # Unlike Initial Balance, a first Restock IS a real purchase event.
    assert body["latest_purchase_unit_cost"] == "4.500000"
    assert body["replacement_unit_cost"] is None
    assert body["effective_replacement_cost"] == "4.500000"

    txns = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory/transactions",
        headers=csrf_headers(client),
    ).json()
    assert txns["total"] == 1
    assert txns["items"][0]["transaction_type"] == "RESTOCK"


def test_restock_with_a_version_but_no_existing_row_reports_state_changed(client):
    signup(client)
    product = _create_product(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"version": 1, "quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_STATE_CHANGED"


def test_restock_without_a_version_after_the_row_already_exists_reports_state_changed(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"quantity": "5", "unit_cost": "2"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_STATE_CHANGED"


def test_first_restock_rejects_an_inactive_product(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_PRODUCT_INACTIVE"


# --- Ordinary Restock against an existing row -------------------------------------------


def test_ordinary_restock_known_weighted_average_calculation(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "2.00"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"version": 1, "quantity": "5", "unit_cost": "5.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["physical_quantity"] == "15.000000"
    assert body["weighted_average_unit_cost"] == "3.000000"
    assert body["latest_purchase_unit_cost"] == "5.000000"


def test_ordinary_restock_rejects_stale_version(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"version": 999, "quantity": "5", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STALE_VERSION"


# --- Manual Adjustment -----------------------------------------------------------------


def test_manual_adjustment_changes_quantity_only(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "20", "unit_cost": "2"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/adjustments",
        json={"version": 1, "quantity_change": "-3", "reason": "DAMAGE", "notes": "Dented cans."},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["physical_quantity"] == "17.000000"
    assert body["weighted_average_unit_cost"] == "2.000000"


def test_manual_adjustment_is_allowed_on_an_inactive_product(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "20", "unit_cost": "2"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/adjustments",
        json={"version": 1, "quantity_change": "-5", "reason": "COUNT_CORRECTION"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["physical_quantity"] == "15.000000"


def test_manual_adjustment_rejects_a_result_below_zero(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "5", "unit_cost": "2"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/adjustments",
        json={"version": 1, "quantity_change": "-10", "reason": "COUNT_CORRECTION"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_INSUFFICIENT_BALANCE"

    # Confirm nothing was mutated by the rejected attempt.
    current = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "5.000000"
    assert current["version"] == 1


def test_manual_adjustment_before_any_initialization_returns_not_initialized(client):
    signup(client)
    product = _create_product(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/adjustments",
        json={"version": 1, "quantity_change": "-1", "reason": "OTHER"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_NOT_INITIALIZED"


# --- Replacement cost --------------------------------------------------------------------


def test_replacement_cost_set_and_clear_round_trip(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "2"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"version": 1, "quantity": "1", "unit_cost": "6"},
        headers=csrf_headers(client),
    )

    set_response = client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 2, "replacement_unit_cost": "8.88"},
        headers=csrf_headers(client),
    )
    assert set_response.status_code == 200, set_response.text
    assert set_response.json()["effective_replacement_cost"] == "8.880000"

    clear_response = client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 3, "replacement_unit_cost": None},
        headers=csrf_headers(client),
    )
    assert clear_response.status_code == 200, clear_response.text
    assert clear_response.json()["replacement_unit_cost"] is None
    assert clear_response.json()["effective_replacement_cost"] == "6.000000"


def test_replacement_cost_rejects_an_inactive_product(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "2"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 1, "replacement_unit_cost": "5"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PURCHASED_INVENTORY_PRODUCT_INACTIVE"


# --- History -----------------------------------------------------------------------------


def test_history_remains_readable_for_an_inactive_product(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory/transactions",
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


# --- Top-level Inventory-module list -----------------------------------------------------


def test_top_level_list_includes_uninitialized_and_initialized_purchased_products(client):
    signup(client)
    uninitialized = _create_product(client, name="Not Yet Initialized")
    initialized = _create_product(client, name="Already Stocked")
    client.post(
        f"/api/v1/products/{initialized['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    # Not listed: a PRODUCED product should never appear here.
    _create_product(client, name="Baked Bread", product_type="PRODUCED")

    response = client.get("/api/v1/purchased-inventory", headers=csrf_headers(client))
    assert response.status_code == 200, response.text
    body = response.json()
    names = {item["product_name"] for item in body["items"]}
    assert names == {"Not Yet Initialized", "Already Stocked"}

    uninitialized_item = next(i for i in body["items"] if i["product_id"] == uninitialized["id"])
    assert uninitialized_item["physical_quantity"] is None
    assert uninitialized_item["version"] is None

    initialized_item = next(i for i in body["items"] if i["product_id"] == initialized["id"])
    assert initialized_item["physical_quantity"] == "10.000000"


def test_top_level_list_includes_inactive_products_by_default(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.get("/api/v1/purchased-inventory", headers=csrf_headers(client))
    assert response.status_code == 200, response.text
    item = next(i for i in response.json()["items"] if i["product_id"] == product["id"])
    assert item["product_is_active"] is False


def test_top_level_list_can_filter_to_active_only(client):
    signup(client)
    active = _create_product(client, name="Active One")
    inactive = _create_product(client, name="Inactive One")
    client.post(
        f"/api/v1/products/{inactive['id']}/archive",
        json={"version": inactive["version"]},
        headers=csrf_headers(client),
    )

    response = client.get(
        "/api/v1/purchased-inventory?is_active=true", headers=csrf_headers(client)
    )
    ids = {item["product_id"] for item in response.json()["items"]}
    assert active["id"] in ids
    assert inactive["id"] not in ids


# --- Finding 2: replacement_unit_cost omission vs explicit null ------------------------


def test_replacement_cost_rejects_an_omitted_field(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 1, "replacement_unit_cost": "5"},
        headers=csrf_headers(client),
    )

    response = client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text

    current = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    ).json()
    assert current["replacement_unit_cost"] == "5.000000"
    assert current["version"] == 2


def test_replacement_cost_explicit_null_clears_but_omission_does_not(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "10", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 1, "replacement_unit_cost": "9"},
        headers=csrf_headers(client),
    )

    omitted = client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert omitted.status_code == 422, omitted.text

    explicit_null = client.put(
        f"/api/v1/products/{product['id']}/purchased-inventory/replacement-cost",
        json={"version": 2, "replacement_unit_cost": None},
        headers=csrf_headers(client),
    )
    assert explicit_null.status_code == 200, explicit_null.text
    assert explicit_null.json()["replacement_unit_cost"] is None


# --- Finding 4: NUMERIC(18,6) representability ------------------------------------------


def test_initial_balance_rejects_a_total_cost_too_large_for_numeric_18_6(client):
    signup(client)
    product = _create_product(client)

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "2000000", "unit_cost": "2000000"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    response2 = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    )
    assert response2.status_code == 404, response2.text


def test_restock_rejects_a_resulting_physical_quantity_too_large_for_numeric_18_6(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "900000000000", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"version": 1, "quantity": "200000000000", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "900000000000.000000"
    assert current["version"] == 1
    txns = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory/transactions",
        headers=csrf_headers(client),
    ).json()
    assert txns["total"] == 1


def test_restock_rejects_a_total_cost_too_large_for_numeric_18_6(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "1", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/restock",
        json={"version": 1, "quantity": "2000000", "unit_cost": "2000000"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "1.000000"
    assert current["version"] == 1


def test_adjustment_rejects_a_resulting_physical_quantity_too_large_for_numeric_18_6(client):
    signup(client)
    product = _create_product(client)
    client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/initial-balance",
        json={"quantity": "999999999999", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/purchased-inventory/adjustments",
        json={"version": 1, "quantity_change": "1", "reason": "OTHER"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/products/{product['id']}/purchased-inventory", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "999999999999.000000"
    assert current["version"] == 1
