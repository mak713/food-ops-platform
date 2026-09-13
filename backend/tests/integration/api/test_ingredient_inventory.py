"""Ingredient physical-inventory API tests (Phase 5 Plan §G): Initial Balance, Restock,
Manual Adjustment, replacement-cost maintenance, and history."""

from __future__ import annotations

from app.db.models.ingredient import InventoryTransaction
from tests.integration.api.helpers import csrf_headers, signup


def _create_ingredient(client, **overrides) -> dict:
    payload = {"name": "Flour", "measurement_family": "WEIGHT", "canonical_unit": "g"}
    payload.update(overrides)
    return client.post("/api/v1/ingredients", json=payload, headers=csrf_headers(client)).json()


# --- Initial Balance -------------------------------------------------------------------


def test_initial_balance_sets_physical_quantity_and_weighted_average_only(client):
    signup(client)
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "5000", "unit": "g", "unit_cost": "0.002"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["physical_quantity"] == "5000.000000"
    assert body["weighted_average_unit_cost"] == "0.002000"
    # Not evidence of an actual purchase event (approval decision 3).
    assert body["latest_purchase_unit_cost"] is None
    assert body["replacement_unit_cost"] is None
    assert body["effective_replacement_cost"] is None
    assert body["version"] == 2

    txns = client.get(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/transactions",
        headers=csrf_headers(client),
    ).json()
    assert txns["total"] == 1
    txn = txns["items"][0]
    assert txn["transaction_type"] == "INITIAL_BALANCE"
    assert txn["quantity_change"] == "5000.000000"
    assert txn["unit_cost"] == "0.002000"
    assert txn["total_cost"] == "10.000000"


def test_initial_balance_normalizes_a_compatible_unit_to_canonical(client):
    signup(client)
    ingredient = _create_ingredient(client, canonical_unit="g")

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "5", "unit": "kg", "unit_cost": "2"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # 5 kg -> 5000 g; $2/kg -> $0.002/g.
    assert body["physical_quantity"] == "5000.000000"
    assert body["weighted_average_unit_cost"] == "0.002000"


def test_initial_balance_rejects_cross_family_unit(client):
    signup(client)
    ingredient = _create_ingredient(client, canonical_unit="g")

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "1", "unit": "mL", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "CROSS_FAMILY_UNIT_CONVERSION"


def test_initial_balance_rejects_a_second_attempt(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "100", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 2, "quantity": "50", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INGREDIENT_INVENTORY_ALREADY_INITIALIZED"


def test_initial_balance_rejects_stale_version(client):
    signup(client)
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 999, "quantity": "100", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STALE_VERSION"


def test_initial_balance_rejects_an_archived_ingredient(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": 1},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 2, "quantity": "100", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INGREDIENT_INVENTORY_ARCHIVED"


# --- Restock -----------------------------------------------------------------------


def test_restock_known_weighted_average_calculation(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "2.00"},
        headers=csrf_headers(client),
    )

    # (10*2 + 5*5) / 15 = 3.00
    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "5", "unit": "g", "unit_cost": "5.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["physical_quantity"] == "15.000000"
    assert body["weighted_average_unit_cost"] == "3.000000"
    assert body["latest_purchase_unit_cost"] == "5.000000"
    assert body["effective_replacement_cost"] == "5.000000"  # falls back to Latest


def test_restock_converts_a_compatible_unit(client):
    signup(client)
    ingredient = _create_ingredient(client, canonical_unit="g")
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "1000", "unit": "g", "unit_cost": "0.01"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "1", "unit": "kg", "unit_cost": "10"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # 1 kg -> 1000 g; $10/kg -> $0.01/g. (1000*0.01 + 1000*0.01)/2000 = 0.01.
    assert body["physical_quantity"] == "2000.000000"
    assert body["weighted_average_unit_cost"] == "0.010000"
    assert body["latest_purchase_unit_cost"] == "0.010000"


def test_restock_rejects_cross_family_unit(client):
    signup(client)
    ingredient = _create_ingredient(client, canonical_unit="g")
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "100", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "1", "unit": "cup", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "CROSS_FAMILY_UNIT_CONVERSION"


def test_restock_on_a_zero_balance_ingredient_resolves_to_purchase_cost(client):
    signup(client)
    ingredient = _create_ingredient(client)
    # No initial balance recorded — the Ingredient still sits at its schema default
    # zero balance, exactly the non-positive-pre-restock-balance edge case (§J-1).

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "4.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["physical_quantity"] == "10.000000"
    assert body["weighted_average_unit_cost"] == "4.000000"


def test_restock_never_touches_an_existing_replacement_cost_override(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 2, "replacement_unit_cost": "9.99"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 3, "quantity": "10", "unit": "g", "unit_cost": "2"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest_purchase_unit_cost"] == "2.000000"
    # The explicit override survives the restock untouched.
    assert body["replacement_unit_cost"] == "9.990000"
    assert body["effective_replacement_cost"] == "9.990000"


def test_restock_rejects_an_archived_ingredient(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": 1},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INGREDIENT_INVENTORY_ARCHIVED"


def test_restock_rejects_stale_version(client):
    signup(client)
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 999, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STALE_VERSION"


# --- Manual Adjustment ---------------------------------------------------------------


def test_manual_adjustment_changes_quantity_only_and_is_traceable(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "100", "unit": "g", "unit_cost": "2"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/adjustments",
        json={
            "version": 2,
            "quantity_change": "-15",
            "reason": "SPOILAGE_OR_WASTE",
            "notes": "Mold found in the bin.",
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["physical_quantity"] == "85.000000"
    # Cost fields are untouched by a manual adjustment.
    assert body["weighted_average_unit_cost"] == "2.000000"
    assert body["latest_purchase_unit_cost"] is None

    txns = client.get(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/transactions",
        headers=csrf_headers(client),
    ).json()
    adjustment = next(t for t in txns["items"] if t["transaction_type"] == "MANUAL_ADJUSTMENT")
    assert adjustment["quantity_change"] == "-15.000000"
    assert adjustment["reason"] == "SPOILAGE_OR_WASTE"
    assert adjustment["notes"] == "Mold found in the bin."
    assert adjustment["unit_cost"] is None
    assert adjustment["total_cost"] is None


def test_manual_adjustment_may_drive_physical_quantity_negative(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/adjustments",
        json={"version": 2, "quantity_change": "-25", "reason": "COUNT_CORRECTION"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["physical_quantity"] == "-15.000000"


def test_manual_adjustment_is_allowed_on_an_archived_ingredient(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": 2},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/adjustments",
        json={"version": 3, "quantity_change": "-5", "reason": "COUNT_CORRECTION"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["physical_quantity"] == "5.000000"


def test_manual_adjustment_rejects_a_zero_quantity_change(client):
    signup(client)
    ingredient = _create_ingredient(client)

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/adjustments",
        json={"version": 1, "quantity_change": "0", "reason": "OTHER"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text


# --- Replacement cost ------------------------------------------------------------------


def test_replacement_cost_defaults_to_latest_purchase_cost_when_unset(client):
    signup(client)
    ingredient = _create_ingredient(client)
    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    # Neither Latest nor Replacement is set yet — effective is unknown.
    assert response.json()["effective_replacement_cost"] is None

    restocked = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "1", "unit": "g", "unit_cost": "7"},
        headers=csrf_headers(client),
    ).json()
    assert restocked["latest_purchase_unit_cost"] == "7.000000"
    assert restocked["replacement_unit_cost"] is None
    assert restocked["effective_replacement_cost"] == "7.000000"


def test_replacement_cost_can_be_set_and_cleared(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "1", "unit": "g", "unit_cost": "7"},
        headers=csrf_headers(client),
    )

    set_response = client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 3, "replacement_unit_cost": "12.34"},
        headers=csrf_headers(client),
    )
    assert set_response.status_code == 200, set_response.text
    assert set_response.json()["replacement_unit_cost"] == "12.340000"
    assert set_response.json()["effective_replacement_cost"] == "12.340000"

    clear_response = client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 4, "replacement_unit_cost": None},
        headers=csrf_headers(client),
    )
    assert clear_response.status_code == 200, clear_response.text
    assert clear_response.json()["replacement_unit_cost"] is None
    # Falls back to Latest Purchase Cost again once cleared.
    assert clear_response.json()["effective_replacement_cost"] == "7.000000"


def test_setting_replacement_cost_does_not_create_a_transaction_row(client, session):
    signup(client)
    ingredient = _create_ingredient(client)
    client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 1, "replacement_unit_cost": "5"},
        headers=csrf_headers(client),
    )
    count = (
        session.query(InventoryTransaction)
        .filter(InventoryTransaction.ingredient_id == ingredient["id"])
        .count()
    )
    assert count == 0


def test_replacement_cost_rejects_an_archived_ingredient(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": 1},
        headers=csrf_headers(client),
    )

    response = client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 2, "replacement_unit_cost": "5"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INGREDIENT_INVENTORY_ARCHIVED"


# --- History pagination ----------------------------------------------------------------


def test_inventory_transaction_history_is_paginated_newest_first(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    for i in range(3):
        client.post(
            f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
            json={"version": 2 + i, "quantity": "1", "unit": "g", "unit_cost": "1"},
            headers=csrf_headers(client),
        )

    page1 = client.get(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/transactions?limit=2&offset=0",
        headers=csrf_headers(client),
    ).json()
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    assert page1["items"][0]["transaction_type"] == "RESTOCK"

    page2 = client.get(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/transactions?limit=2&offset=2",
        headers=csrf_headers(client),
    ).json()
    assert len(page2["items"]) == 2
    assert page2["items"][-1]["transaction_type"] == "INITIAL_BALANCE"


def test_history_remains_readable_for_an_archived_ingredient(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": 2},
        headers=csrf_headers(client),
    )

    response = client.get(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/transactions",
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


# --- Finding 2: replacement_unit_cost omission vs explicit null ------------------------


def test_replacement_cost_rejects_an_omitted_field(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    # Set an override first, so an accidental clear would be observable.
    client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 2, "replacement_unit_cost": "5"},
        headers=csrf_headers(client),
    )

    response = client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 3},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text

    # The existing override survives — omission never clears it (Phase 5 correction
    # pass finding 2).
    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["replacement_unit_cost"] == "5.000000"
    assert current["version"] == 3


def test_replacement_cost_explicit_null_clears_but_omission_does_not(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "10", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 2, "replacement_unit_cost": "9"},
        headers=csrf_headers(client),
    )

    omitted = client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 3},
        headers=csrf_headers(client),
    )
    assert omitted.status_code == 422, omitted.text

    explicit_null = client.put(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/replacement-cost",
        json={"version": 3, "replacement_unit_cost": None},
        headers=csrf_headers(client),
    )
    assert explicit_null.status_code == 200, explicit_null.text
    assert explicit_null.json()["replacement_unit_cost"] is None


# --- Finding 4: NUMERIC(18,6) representability ------------------------------------------


def test_initial_balance_rejects_a_positive_quantity_that_rounds_to_zero(client):
    signup(client)
    # canonical_unit "kg" is larger than the entered unit "g" by a factor of 1000 —
    # the smallest representable positive quantity ("0.000001") converts to
    # 0.000000001 kg, which quantizes to exactly zero.
    ingredient = _create_ingredient(client, canonical_unit="kg")

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "0.000001", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "QUANTITY_TOO_SMALL"

    # Nothing was initialized.
    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "0.000000"
    assert current["version"] == 1


def test_restock_rejects_a_positive_quantity_that_rounds_to_zero(client):
    signup(client)
    ingredient = _create_ingredient(client, canonical_unit="kg")
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "1", "unit": "kg", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "0.000001", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "QUANTITY_TOO_SMALL"

    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "1.000000"
    assert current["version"] == 2


def test_initial_balance_rejects_a_converted_quantity_too_large_for_numeric_18_6(client):
    signup(client)
    # canonical_unit "g" with an entered unit of "kg" (factor 1000) — the maximum raw
    # request value, once converted, overflows NUMERIC(18,6)'s 12-integer-digit shape.
    ingredient = _create_ingredient(client, canonical_unit="g")

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "999999999999.999999", "unit": "kg", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "0.000000"
    assert current["version"] == 1


def test_restock_rejects_a_converted_unit_cost_too_large_for_numeric_18_6(client):
    signup(client)
    # canonical_unit "kg" with an entered unit of "g" (factor 1000, applied to the cost
    # in the opposite direction from a quantity conversion) — a small, valid quantity but
    # a maximal unit cost overflows once converted to "per kg."
    ingredient = _create_ingredient(client, canonical_unit="kg")
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "1", "unit": "kg", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "1", "unit": "g", "unit_cost": "999999999999.999999"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "1.000000"
    assert current["version"] == 2


def test_restock_rejects_a_resulting_physical_quantity_too_large_for_numeric_18_6(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "900000000000", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "200000000000", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    # Balance, version, and history are all untouched by the rejected mutation.
    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "900000000000.000000"
    assert current["version"] == 2
    txns = client.get(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/transactions",
        headers=csrf_headers(client),
    ).json()
    assert txns["total"] == 1


def test_restock_rejects_a_total_cost_too_large_for_numeric_18_6(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "1", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    # 2,000,000 * 2,000,000 = 4e12, which exceeds NUMERIC(18,6)'s ~1e12 ceiling, while
    # each factor and the resulting physical quantity individually stay in range.
    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/restock",
        json={"version": 2, "quantity": "2000000", "unit": "g", "unit_cost": "2000000"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "1.000000"
    assert current["version"] == 2


def test_adjustment_rejects_a_resulting_physical_quantity_too_large_for_numeric_18_6(client):
    signup(client)
    ingredient = _create_ingredient(client)
    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/initial-balance",
        json={"version": 1, "quantity": "999999999999", "unit": "g", "unit_cost": "1"},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/ingredients/{ingredient['id']}/inventory/adjustments",
        json={"version": 2, "quantity_change": "1", "reason": "OTHER"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALUE_OUT_OF_RANGE"

    current = client.get(
        f"/api/v1/ingredients/{ingredient['id']}", headers=csrf_headers(client)
    ).json()
    assert current["physical_quantity"] == "999999999999.000000"
    assert current["version"] == 2
