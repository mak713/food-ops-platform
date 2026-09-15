"""Order/OrderLine API integration tests (Phase 6 Final Plan §J; Final
Pre-Implementation Amendment)."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models.business import Business
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.product import Product, SellingOption
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import make_customer, make_product


def _create_product_with_option(client, *, price="12.00", quantity_units=6, packaging_cost="0"):
    product = client.post(
        "/api/v1/products",
        json={"name": "Sourdough Loaf", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    option = client.post(
        f"/api/v1/products/{product['id']}/selling-options",
        json={
            "name": "6-pack",
            "quantity_units": quantity_units,
            "price": price,
            "packaging_cost": packaging_cost,
        },
        headers=csrf_headers(client),
    ).json()
    return product, option


def _standard_option_line(product, option, **overrides):
    line = {
        "line_type": "STANDARD_OPTION",
        "product_id": product["id"],
        "selling_option_id": option["id"],
        "package_quantity": "2",
    }
    line.update(overrides)
    return line


def _custom_quantity_line(product, **overrides):
    line = {
        "line_type": "CUSTOM_QUANTITY",
        "product_id": product["id"],
        "underlying_quantity": "30",
        "charged_unit_price": "55.00",
    }
    line.update(overrides)
    return line


def _custom_item_line(**overrides):
    line = {
        "line_type": "CUSTOM_ITEM",
        "underlying_quantity": "3",
        "charged_unit_price": "10.00",
        "display_name": "Custom cake topper",
    }
    line.update(overrides)
    return line


# --- Create ------------------------------------------------------------------------------


def test_create_empty_draft_order(client):
    signup(client)
    response = client.post("/api/v1/orders", json={}, headers=csrf_headers(client))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["lines"] == []
    assert body["subtotal"] == "0.00"
    assert body["final_total"] == "0.00"
    assert body["version"] == 1
    assert body["order_number"].startswith("ORD-")
    assert len(body["order_number"]) == 16  # "ORD-" + 12 hex chars
    assert body["is_confirmable"] is False
    codes = [i["code"] for i in body["confirmation_issues"]]
    assert "ORDER_NO_LINES" in codes
    assert "ORDER_MISSING_FULFILLMENT_DATE" in codes
    assert body["estimated_direct_cost"] is None
    assert body["estimated_contribution"] is None
    assert body["estimated_contribution_margin"] is None


def test_create_draft_with_standard_option_line_worked_example(client):
    signup(client)
    product, option = _create_product_with_option(client, price="12.00", quantity_units=6)
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option, package_quantity="2")]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    line = response.json()["lines"][0]
    assert line["package_quantity"] == "2.000000"
    assert line["underlying_quantity"] == "12.000000"
    assert line["charged_unit_price_snapshot"] == "12.00"
    assert line["line_subtotal"] == "24.00"
    assert response.json()["subtotal"] == "24.00"
    assert response.json()["final_total"] == "24.00"


def test_create_draft_with_custom_quantity_line_worked_example(client):
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Chocolate Chip Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_quantity_line(product)]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    line = response.json()["lines"][0]
    assert line["package_quantity"] == "1.000000"
    assert line["underlying_quantity"] == "30.000000"
    assert line["charged_unit_price_snapshot"] == "55.00"
    assert line["line_subtotal"] == "55.00"
    assert line["product_id"] == product["id"]
    assert line["selling_option_id"] is None


def test_create_draft_with_custom_item_line_defaults_manual_fulfillment_true(client):
    signup(client)
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_item_line()]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    line = response.json()["lines"][0]
    assert line["product_id"] is None
    assert line["selling_option_id"] is None
    assert line["package_quantity"] == "3.000000"
    assert line["underlying_quantity"] == "3.000000"
    assert line["line_subtotal"] == "30.00"
    assert line["packaging_cost_per_package_snapshot"] == "0.00"
    assert line["packaging_cost_total_snapshot"] == "0.00"
    # Amendment §7 — backend-enforced default, omitted field still persists true.
    assert line["manual_fulfillment_required"] is True
    assert line["manual_fulfillment_satisfied"] is False


def test_create_draft_custom_item_manual_fulfillment_can_be_explicitly_false(client):
    signup(client)
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_item_line(manual_fulfillment_required=False)]},
        headers=csrf_headers(client),
    )
    assert response.json()["lines"][0]["manual_fulfillment_required"] is False


def test_create_draft_standard_option_and_custom_quantity_force_manual_fulfillment_false(client):
    signup(client)
    product, option = _create_product_with_option(client)
    custom_qty_product = client.post(
        "/api/v1/products",
        json={"name": "Bagel", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    response = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                _standard_option_line(product, option),
                _custom_quantity_line(custom_qty_product),
            ]
        },
        headers=csrf_headers(client),
    )
    for line in response.json()["lines"]:
        assert line["manual_fulfillment_required"] is False
        assert line["manual_fulfillment_satisfied"] is False


def test_create_draft_with_customer(client, session):
    signup(client)
    customer_resp = client.post(
        "/api/v1/customers", json={"name": "Grace Hopper"}, headers=csrf_headers(client)
    ).json()
    response = client.post(
        "/api/v1/orders",
        json={"customer_id": customer_resp["id"]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201
    assert response.json()["customer_id"] == customer_resp["id"]


def test_create_draft_guest_order_has_null_customer(client):
    signup(client)
    response = client.post("/api/v1/orders", json={}, headers=csrf_headers(client))
    assert response.json()["customer_id"] is None


def test_create_draft_rejects_missing_customer_non_disclosing(client):
    signup(client)
    response = client.post(
        "/api/v1/orders",
        json={"customer_id": "00000000-0000-0000-0000-000000000000"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "CUSTOMER_NOT_FOUND"


def test_create_draft_rejects_missing_product_non_disclosing(client):
    signup(client)
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_quantity_line({"id": "00000000-0000-0000-0000-000000000000"})]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "PRODUCT_NOT_FOUND"


def test_create_draft_rejects_selling_option_belonging_to_wrong_product_non_disclosing(client):
    signup(client)
    product_a, option_a = _create_product_with_option(client)
    product_b, _ = _create_product_with_option(client)
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product_b, option_a)]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "SELLING_OPTION_NOT_FOUND"


def test_create_draft_rejects_new_reference_to_archived_product(client):
    signup(client)
    product, option = _create_product_with_option(client)
    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "PRODUCT_NOT_ACTIVE"


def test_create_draft_lines_from_selling_options_under_different_products(client):
    """A single Order may contain STANDARD_OPTION lines referencing Selling Options under
    different Products — the lock query must not assume a single shared product_id
    (Final Pre-Implementation Amendment §12)."""
    signup(client)
    product_a, option_a = _create_product_with_option(client)
    product_b, option_b = _create_product_with_option(client)
    response = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                _standard_option_line(product_a, option_a),
                _standard_option_line(product_b, option_b),
            ]
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    assert len(response.json()["lines"]) == 2


def test_create_draft_adjustment_requires_description(client):
    signup(client)
    response = client.post(
        "/api/v1/orders", json={"order_adjustment": "-5.00"}, headers=csrf_headers(client)
    )
    assert response.status_code == 422


def test_create_draft_adjustment_with_description_applies_to_final_total(client):
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    response = client.post(
        "/api/v1/orders",
        json={
            "lines": [_custom_quantity_line(product)],
            "order_adjustment": "-5.00",
            "adjustment_description": "Loyalty discount",
            "manual_tax": "2.00",
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subtotal"] == "55.00"
    assert body["final_total"] == "52.00"  # 55 - 5 + 2


def test_create_draft_negative_final_total_rejected(client):
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    response = client.post(
        "/api/v1/orders",
        json={
            "lines": [_custom_quantity_line(product, charged_unit_price="10.00")],
            "order_adjustment": "-50.00",
            "adjustment_description": "Big discount",
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ORDER_FINAL_TOTAL_NEGATIVE"


def test_create_draft_writes_null_to_draft_history_row_with_changed_at(client, session):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    row = session.execute(
        select(OrderStatusHistory).where(OrderStatusHistory.order_id == created["id"])
    ).scalar_one()
    assert row.from_status is None
    assert row.to_status.value == "DRAFT"
    assert row.changed_at is not None


def test_create_draft_order_number_format(client):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    number = created["order_number"]
    assert number.startswith("ORD-")
    hex_part = number.removeprefix("ORD-")
    assert len(hex_part) == 12
    int(hex_part, 16)  # raises if not valid hex
    assert hex_part == hex_part.upper()


# --- Read / list --------------------------------------------------------------------------


def test_get_and_list_orders(client):
    signup(client)
    client.post("/api/v1/orders", json={}, headers=csrf_headers(client))
    client.post("/api/v1/orders", json={}, headers=csrf_headers(client))
    listing = client.get("/api/v1/orders")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2

    order_id = listing.json()["items"][0]["id"]
    detail = client.get(f"/api/v1/orders/{order_id}")
    assert detail.status_code == 200


def test_list_orders_filters_by_status_and_customer(client):
    signup(client)
    customer = client.post(
        "/api/v1/customers", json={"name": "A Customer"}, headers=csrf_headers(client)
    ).json()
    client.post(
        "/api/v1/orders", json={"customer_id": customer["id"]}, headers=csrf_headers(client)
    )
    client.post("/api/v1/orders", json={}, headers=csrf_headers(client))

    by_customer = client.get(f"/api/v1/orders?customer_id={customer['id']}")
    assert by_customer.json()["total"] == 1

    by_status = client.get("/api/v1/orders?status=DRAFT")
    assert by_status.json()["total"] == 2


# --- Stable-ID line reconciliation ---------------------------------------------------------


def test_update_retains_line_id_new_gets_new_id_deleted_line_disappears(client):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                _standard_option_line(product, option),
                _custom_item_line(display_name="Extra"),
            ]
        },
        headers=csrf_headers(client),
    ).json()
    # Both lines share an identical transaction-time `created_at`, so their relative
    # order in the response is not guaranteed by insertion order — identify each by type.
    retained_id = next(
        line["id"] for line in created["lines"] if line["line_type"] == "STANDARD_OPTION"
    )
    dropped_id = next(line["id"] for line in created["lines"] if line["line_type"] == "CUSTOM_ITEM")

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {**_standard_option_line(product, option), "id": retained_id},
                _custom_item_line(display_name="Brand new"),
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    ids = [line["id"] for line in body["lines"]]
    assert retained_id in ids
    assert dropped_id not in ids
    new_ids = [i for i in ids if i != retained_id]
    assert len(new_ids) == 1
    assert new_ids[0] != dropped_id


def test_update_rejects_duplicate_submitted_line_id(client):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    response = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {**_standard_option_line(product, option), "id": line_id},
                {**_standard_option_line(product, option), "id": line_id},
            ],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "DUPLICATE_ORDER_LINE_ID"


def test_update_rejects_line_id_from_another_order(client):
    signup(client)
    product, option = _create_product_with_option(client)
    order_a = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    ).json()
    order_b = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()

    response = client.patch(
        f"/api/v1/orders/{order_b['id']}",
        json={
            "version": order_b["version"],
            "lines": [{**_standard_option_line(product, option), "id": order_a["lines"][0]["id"]}],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "UNKNOWN_ORDER_LINE_ID"


def test_update_non_draft_order_is_rejected(client, session):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    order_row = session.execute(select(Order).where(Order.id == created["id"])).scalar_one()
    order_row.status = "CONFIRMED"
    session.commit()
    session.refresh(order_row)  # version_id_col bumps on this direct mutation too

    response = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={"version": order_row.version},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_DRAFT"


# --- Independent snapshot-edit-class behavior (STANDARD_OPTION) ------------------------


def test_quantity_only_edit_does_not_pick_up_catalog_change(client, session):
    signup(client)
    product, option = _create_product_with_option(client, price="12.00", quantity_units=6)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option, package_quantity="2")]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    # Live catalog change AFTER the line was captured.
    option_row = session.execute(
        select(SellingOption).where(SellingOption.id == option["id"])
    ).scalar_one()
    option_row.price = "99.00"
    option_row.packaging_cost = "5.00"
    option_row.quantity_units = "100"
    session.commit()

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {**_standard_option_line(product, option, package_quantity="3"), "id": line_id}
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    line = updated.json()["lines"][0]
    assert line["package_quantity"] == "3.000000"
    # ratio 12/2=6 units/package reapplied: 3*6=18, never adopting the new 100 multiplier.
    assert line["underlying_quantity"] == "18.000000"
    assert line["charged_unit_price_snapshot"] == "12.00"  # untouched
    assert line["packaging_cost_per_package_snapshot"] == "0.00"  # untouched


def test_price_only_edit_does_not_touch_packaging_name_or_quantity(client, session):
    signup(client)
    product, option = _create_product_with_option(client, price="12.00", quantity_units=6)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option, package_quantity="2")]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]
    original_name = created["lines"][0]["display_name_snapshot"]

    option_row = session.execute(
        select(SellingOption).where(SellingOption.id == option["id"])
    ).scalar_one()
    option_row.packaging_cost = "5.00"
    option_row.quantity_units = "100"
    session.commit()

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {
                    **_standard_option_line(product, option, package_quantity="2"),
                    "id": line_id,
                    "charged_unit_price": "20.00",
                    "price_override_reason": "Special request",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    line = updated.json()["lines"][0]
    assert line["charged_unit_price_snapshot"] == "20.00"
    assert line["price_override_reason"] == "Special request"
    assert line["line_subtotal"] == "40.00"
    assert line["display_name_snapshot"] == original_name
    assert line["underlying_quantity"] == "12.000000"  # untouched, not the new x100
    assert line["packaging_cost_per_package_snapshot"] == "0.00"  # untouched


def test_resubmitting_same_selling_option_is_not_a_source_change(client, session):
    """Final Pre-Implementation Amendment §5: same-ID reselection must not refresh
    anything from the current catalog, even if every other field is resubmitted."""
    signup(client)
    product, option = _create_product_with_option(client, price="12.00", quantity_units=6)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option, package_quantity="2")]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    option_row = session.execute(
        select(SellingOption).where(SellingOption.id == option["id"])
    ).scalar_one()
    option_row.price = "999.00"
    session.commit()

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {**_standard_option_line(product, option, package_quantity="2"), "id": line_id}
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    line = updated.json()["lines"][0]
    assert line["charged_unit_price_snapshot"] == "12.00"  # unchanged despite resubmission


def test_actual_source_change_to_different_selling_option_fully_recaptures(client):
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Bagel", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    option_a = client.post(
        f"/api/v1/products/{product['id']}/selling-options",
        json={"name": "6-pack", "quantity_units": 6, "price": "12.00", "packaging_cost": "0.50"},
        headers=csrf_headers(client),
    ).json()
    option_b = client.post(
        f"/api/v1/products/{product['id']}/selling-options",
        json={"name": "12-pack", "quantity_units": 12, "price": "20.00", "packaging_cost": "1.00"},
        headers=csrf_headers(client),
    ).json()

    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option_a, package_quantity="2")]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {**_standard_option_line(product, option_b, package_quantity="2"), "id": line_id}
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    line = updated.json()["lines"][0]
    assert line["selling_option_id"] == option_b["id"]
    assert line["underlying_quantity"] == "24.000000"  # 2 * 12
    assert line["charged_unit_price_snapshot"] == "20.00"
    assert line["packaging_cost_per_package_snapshot"] == "1.00"


def test_unrelated_edit_leaves_line_row_completely_untouched(client, session):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "internal_notes": "Changed only this",
            "lines": [{**_standard_option_line(product, option), "id": line_id}],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["lines"][0] == created["lines"][0]
    assert updated.json()["internal_notes"] == "Changed only this"


# --- CUSTOM_QUANTITY source-change semantics --------------------------------------------


def test_custom_quantity_product_source_change_resets_packaging_default(client):
    signup(client)
    product_a = client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED", "default_packaging_cost": "0.10"},
        headers=csrf_headers(client),
    ).json()
    product_b = client.post(
        "/api/v1/products",
        json={"name": "Muffin", "product_type": "PRODUCED", "default_packaging_cost": "0.75"},
        headers=csrf_headers(client),
    ).json()

    created = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_quantity_line(product_a)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]
    assert created["lines"][0]["packaging_cost_per_package_snapshot"] == "0.10"

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [{**_custom_quantity_line(product_b), "id": line_id}],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    line = updated.json()["lines"][0]
    assert line["product_id"] == product_b["id"]
    assert line["display_name_snapshot"] == "Muffin"
    assert line["packaging_cost_per_package_snapshot"] == "0.75"
    # No Product catalog price exists — the price stays exactly what the seller submitted.
    assert line["charged_unit_price_snapshot"] == "55.00"


def test_custom_quantity_packaging_override_survives_source_change(client):
    signup(client)
    product_a = client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    product_b = client.post(
        "/api/v1/products",
        json={"name": "Muffin", "product_type": "PRODUCED", "default_packaging_cost": "0.75"},
        headers=csrf_headers(client),
    ).json()
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_quantity_line(product_a)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {
                    **_custom_quantity_line(product_b, packaging_cost_per_package="0.20"),
                    "id": line_id,
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.json()["lines"][0]["packaging_cost_per_package_snapshot"] == "0.20"


# --- Delete --------------------------------------------------------------------------------


def test_delete_draft_with_no_payments_succeeds(client):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    response = client.delete(
        f"/api/v1/orders/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 204


def test_delete_draft_with_payments_warns_then_confirms(client):
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_quantity_line(product)]},
        headers=csrf_headers(client),
    ).json()
    payment_response = client.post(
        f"/api/v1/orders/{created['id']}/payments",
        json={"amount": "10.00", "payment_method": "cash", "payment_date": "2026-01-01"},
        headers=csrf_headers(client),
    )
    assert payment_response.status_code == 201, payment_response.text
    order_after_payment = client.get(f"/api/v1/orders/{created['id']}").json()

    warned = client.delete(
        f"/api/v1/orders/{created['id']}?version={order_after_payment['version']}",
        headers=csrf_headers(client),
    )
    assert warned.status_code == 422
    assert warned.json()["error"]["code"] == "ORDER_DELETE_HAS_PAYMENTS_WARNING"
    assert warned.json()["error"]["issues"][0]["code"] == "ORDER_HAS_PAYMENTS"

    confirmed = client.delete(
        f"/api/v1/orders/{created['id']}?version={order_after_payment['version']}"
        "&confirm_delete_with_payments=true",
        headers=csrf_headers(client),
    )
    assert confirmed.status_code == 204


def test_delete_non_draft_order_is_rejected(client, session):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    order_row = session.execute(select(Order).where(Order.id == created["id"])).scalar_one()
    order_row.status = "CONFIRMED"
    session.commit()
    session.refresh(order_row)  # version_id_col bumps on this direct mutation too

    response = client.delete(
        f"/api/v1/orders/{created['id']}?version={order_row.version}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_DRAFT"


def test_delete_stale_version_returns_409(client):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    response = client.delete(
        f"/api/v1/orders/{created['id']}?version={created['version'] + 1}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"


# --- Lifecycle boundary: Phase 6 never persists CONFIRMED, never writes Phase 7+ rows ---


def test_no_confirm_route_exists(client):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    response = client.post(
        f"/api/v1/orders/{created['id']}/confirm", json={}, headers=csrf_headers(client)
    )
    assert response.status_code in (404, 405)


def test_confirmation_readiness_reflects_structural_state_without_mutating(client):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [_standard_option_line(product, option)],
            "fulfillment_date": "2026-02-01",
        },
        headers=csrf_headers(client),
    ).json()
    assert created["is_confirmable"] is True
    assert created["confirmation_issues"] == []
    assert created["status"] == "DRAFT"


def test_confirmation_readiness_never_rejects_carried_forward_archived_reference(client, session):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [_standard_option_line(product, option)],
            "fulfillment_date": "2026-02-01",
        },
        headers=csrf_headers(client),
    ).json()

    product_row = session.execute(select(Product).where(Product.id == product["id"])).scalar_one()
    product_row.is_active = False
    session.commit()

    detail = client.get(f"/api/v1/orders/{created['id']}")
    assert detail.json()["is_confirmable"] is True
    assert detail.json()["confirmation_issues"] == []


# --- Tenant isolation --------------------------------------------------------------------


def _signup_two_businesses(client):
    body_a = signup(client, email="order-tenant-a@example.com")
    session_a = client.cookies.get("fo_session")
    csrf_a = client.cookies.get("fo_csrf")
    client.cookies.clear()
    body_b = signup(client, email="order-tenant-b@example.com")
    session_b = client.cookies.get("fo_session")
    csrf_b = client.cookies.get("fo_csrf")
    return {
        "a": {"body": body_a, "session": session_a, "csrf": csrf_a},
        "b": {"body": body_b, "session": session_b, "csrf": csrf_b},
    }


def _use_session(client, which: dict) -> None:
    client.cookies.clear()
    client.cookies.set("fo_session", which["session"])
    client.cookies.set("fo_csrf", which["csrf"])


def test_foreign_tenant_order_get_returns_non_revealing_404(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()

    _use_session(client, tenants["b"])
    response = client.get(f"/api/v1/orders/{created['id']}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_order_list_never_leaks_foreign_tenant_rows(client):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    client.post("/api/v1/orders", json={}, headers=csrf_headers(client))

    _use_session(client, tenants["b"])
    listing = client.get("/api/v1/orders")
    assert listing.json()["total"] == 0


def test_order_create_rejects_foreign_tenant_customer_id_non_disclosing(client, session):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    business_a_id = tenants["a"]["body"]["business"]["id"]
    business_a = session.get(Business, business_a_id)
    foreign_customer = make_customer(session, business_a, name="Foreign Customer")
    session.commit()

    _use_session(client, tenants["b"])
    response = client.post(
        "/api/v1/orders",
        json={"customer_id": str(foreign_customer.id)},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "CUSTOMER_NOT_FOUND"


def test_order_create_rejects_foreign_tenant_product_non_disclosing(client, session):
    tenants = _signup_two_businesses(client)
    _use_session(client, tenants["a"])
    business_a_id = tenants["a"]["body"]["business"]["id"]
    business_a = session.get(Business, business_a_id)
    foreign_product = make_product(session, business_a)
    session.commit()

    _use_session(client, tenants["b"])
    response = client.post(
        "/api/v1/orders",
        json={"lines": [_custom_quantity_line({"id": str(foreign_product.id)})]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "PRODUCT_NOT_FOUND"


def test_archived_selling_option_carried_forward_from_existing_line_is_preserved(client, session):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    client.post(
        f"/api/v1/products/{product['id']}/selling-options/{option['id']}/archive",
        json={"version": option["version"]},
        headers=csrf_headers(client),
    )

    # An unrelated edit must not be blocked by the now-archived, carried-forward reference.
    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "internal_notes": "still fine",
            "lines": [{**_standard_option_line(product, option), "id": line_id}],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["lines"][0]["selling_option_id"] == option["id"]


# --- Checkpoint-3 correction 13: dedicated ORDER_LINE_TYPE_IMMUTABLE regression test ---


def test_update_rejects_changing_an_existing_lines_type_in_place(client, session):
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]
    original_line_row_snapshot = created["lines"][0]

    response = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {
                    "id": line_id,
                    "line_type": "CUSTOM_ITEM",
                    "underlying_quantity": "1",
                    "charged_unit_price": "5.00",
                    "display_name": "Swapped type",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ORDER_LINE_TYPE_IMMUTABLE"
    assert response.json()["error"]["issues"][0]["code"] == "ORDER_LINE_TYPE_IMMUTABLE"

    # The stored line is completely unchanged, and no partial aggregate mutation
    # occurred — the Order is still at its original version.
    detail = client.get(f"/api/v1/orders/{created['id']}").json()
    assert detail["version"] == created["version"]
    assert detail["lines"][0] == original_line_row_snapshot


# --- Checkpoint-3 correction 7: Standard Option partial-source-change locking/validation ---


def test_standard_option_product_change_with_retained_selling_option_returns_clean_422(client):
    """Case B: the Selling Option id is resubmitted unchanged, but the Product changes —
    the (unchanged) Selling Option cannot belong to the new Product, and this must be a
    clean structured 422, never a missing-key crash."""
    signup(client)
    product_a, option_a = _create_product_with_option(client)
    product_b, _option_b = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product_a, option_a)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    response = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {
                    "id": line_id,
                    "line_type": "STANDARD_OPTION",
                    "product_id": product_b["id"],
                    "selling_option_id": option_a["id"],
                    "package_quantity": "2",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "SELLING_OPTION_NOT_FOUND"


def test_standard_option_selling_option_change_under_archived_product_is_rejected(client):
    """Case A: the Product id is unchanged (and was valid when the line was originally
    captured), but has since been archived; the seller now tries to select a *different*
    Selling Option under that same, now-archived Product — this is a new source selection
    and must be rejected, not silently permitted merely because the Product's own id
    didn't change on this request."""
    signup(client)
    product, option_a = _create_product_with_option(client)
    option_b = client.post(
        f"/api/v1/products/{product['id']}/selling-options",
        json={"name": "12-pack", "quantity_units": 12, "price": "20.00"},
        headers=csrf_headers(client),
    ).json()
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option_a)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    client.post(
        f"/api/v1/products/{product['id']}/archive",
        json={"version": product["version"]},
        headers=csrf_headers(client),
    )

    response = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                {
                    "id": line_id,
                    "line_type": "STANDARD_OPTION",
                    "product_id": product["id"],
                    "selling_option_id": option_b["id"],
                    "package_quantity": "2",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "PRODUCT_NOT_ACTIVE"


def test_standard_option_same_archived_product_and_option_no_source_change_remains_valid(
    client, session
):
    """The exact same archived Product+Selling Option pair, resubmitted unchanged (no
    source change at all), must remain valid as a carried-forward reference — only a
    genuinely new source selection is rejected."""
    signup(client)
    product, option = _create_product_with_option(client)
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option)]},
        headers=csrf_headers(client),
    ).json()
    line_id = created["lines"][0]["id"]

    product_row = session.execute(select(Product).where(Product.id == product["id"])).scalar_one()
    product_row.is_active = False
    session.commit()

    response = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "internal_notes": "unrelated edit",
            "lines": [{**_standard_option_line(product, option), "id": line_id}],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["lines"][0]["product_id"] == product["id"]


# --- Checkpoint-3 correction 8: post-lock reconciliation errors roll back the lock -----


def test_reconciliation_overflow_after_locks_rolls_back_and_releases_the_order_lock(
    client, session
):
    """A downstream overflow error (raised well after the reference-validation locks
    succeeded) must roll back — a second, independent connection must be able to
    immediately acquire the affected Order row's lock afterward."""
    signup(client)
    product, option = _create_product_with_option(client, price="999999999999.99")
    created = client.post(
        "/api/v1/orders",
        json={"lines": [_standard_option_line(product, option, package_quantity="1")]},
        headers=csrf_headers(client),
    ).json()
    order_id = created["id"]

    # A package_quantity large enough that package_quantity * price overflows
    # NUMERIC(14,2) triggers the reconciliation-stage ORDER_LINE_VALUE_OVERFLOW error,
    # which is raised well after this request's own reference locks succeeded.
    response = client.patch(
        f"/api/v1/orders/{order_id}",
        json={
            "version": created["version"],
            "lines": [
                {
                    "id": created["lines"][0]["id"],
                    "line_type": "STANDARD_OPTION",
                    "product_id": product["id"],
                    "selling_option_id": option["id"],
                    "package_quantity": "999999999999.99",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ORDER_LINE_VALUE_OVERFLOW"

    # A second, independent connection can immediately acquire the same row's lock —
    # proof the failed request's rollback actually released it.
    from sqlalchemy import select as sa_select

    from app.db.models.order import Order as OrderModel

    locked = session.scalar(
        sa_select(OrderModel).where(OrderModel.id == order_id).with_for_update(nowait=True)
    )
    assert locked is not None
    session.rollback()


# --- Checkpoint-3 correction 10: subtotal representability validated independently ----


def test_create_draft_subtotal_overflow_rejected_even_when_final_total_representable(client):
    """Two individually-representable line subtotals whose SUM overflows NUMERIC(14,2)
    must be rejected even though a large negative adjustment would otherwise bring
    final_total back into a representable range."""
    signup(client)
    product_a = client.post(
        "/api/v1/products",
        json={"name": "Line A", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    product_b = client.post(
        "/api/v1/products",
        json={"name": "Line B", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    response = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": product_a["id"],
                    "underlying_quantity": "1",
                    "charged_unit_price": "600000000000.00",
                },
                {
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": product_b["id"],
                    "underlying_quantity": "1",
                    "charged_unit_price": "600000000000.00",
                },
            ],
            "order_adjustment": "-300000000000.00",
            "adjustment_description": "Huge discount bringing final_total back in range",
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ORDER_LINE_VALUE_OVERFLOW"


# --- Checkpoint-3 correction 15: Customer archived/new-reference carry-forward --------


def test_inactive_customer_cannot_be_newly_selected(client, session):
    signup(client)
    customer = client.post(
        "/api/v1/customers", json={"name": "Archived Customer"}, headers=csrf_headers(client)
    ).json()
    client.post(
        f"/api/v1/customers/{customer['id']}/archive",
        json={"version": customer["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"]},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "CUSTOMER_NOT_ACTIVE"


def test_customer_carried_forward_through_unrelated_edit_after_becoming_inactive(client, session):
    signup(client)
    customer = client.post(
        "/api/v1/customers", json={"name": "Loyal Customer"}, headers=csrf_headers(client)
    ).json()
    created = client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"]},
        headers=csrf_headers(client),
    ).json()

    client.post(
        f"/api/v1/customers/{customer['id']}/archive",
        json={"version": customer["version"]},
        headers=csrf_headers(client),
    )

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "customer_id": customer["id"],
            "internal_notes": "unrelated edit",
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["customer_id"] == customer["id"]


def test_customer_cannot_be_reselected_while_inactive_after_being_removed(client, session):
    signup(client)
    customer = client.post(
        "/api/v1/customers", json={"name": "Once Selected"}, headers=csrf_headers(client)
    ).json()
    created = client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"]},
        headers=csrf_headers(client),
    ).json()

    client.post(
        f"/api/v1/customers/{customer['id']}/archive",
        json={"version": customer["version"]},
        headers=csrf_headers(client),
    )

    # Remove the customer reference (guest order) — a legitimate unrelated-ish edit.
    removed = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={"version": created["version"], "customer_id": None},
        headers=csrf_headers(client),
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["customer_id"] is None

    # Re-selecting the now-inactive customer again is a NEW reference and is rejected.
    reselected = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={"version": removed.json()["version"], "customer_id": customer["id"]},
        headers=csrf_headers(client),
    )
    assert reselected.status_code == 422
    assert reselected.json()["error"]["issues"][0]["code"] == "CUSTOMER_NOT_ACTIVE"


# --- Checkpoint-3 correction 14: zero Phase 7+ writes from representative operations ---


def test_phase6_operations_create_zero_phase7_operational_rows(client, session):
    from sqlalchemy import func as sa_func

    from app.db.models.cost import OrderCostAllocation
    from app.db.models.production import (
        IngredientReservation,
        ProductionRequirement,
        ProductionRequirementOrder,
        ProductionRun,
        ProductionRunOrderAllocation,
    )
    from app.db.models.surplus import PurchasedProductReservation, SurplusAllocation

    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Phase7 Guard Product", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    # Draft create
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": product["id"],
                    "underlying_quantity": "5",
                    "charged_unit_price": "25.00",
                }
            ]
        },
        headers=csrf_headers(client),
    ).json()

    # Draft update
    client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "internal_notes": "phase 7 guard",
            "lines": [
                {
                    "id": created["lines"][0]["id"],
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": product["id"],
                    "underlying_quantity": "6",
                    "charged_unit_price": "25.00",
                }
            ],
        },
        headers=csrf_headers(client),
    )

    # Payment add
    client.post(
        f"/api/v1/orders/{created['id']}/payments",
        json={"amount": "10.00", "payment_method": "cash", "payment_date": "2026-01-01"},
        headers=csrf_headers(client),
    )

    for model in (
        ProductionRequirement,
        ProductionRequirementOrder,
        IngredientReservation,
        PurchasedProductReservation,
        SurplusAllocation,
        ProductionRun,
        ProductionRunOrderAllocation,
        OrderCostAllocation,
    ):
        count = session.scalar(select(sa_func.count()).select_from(model))
        assert count == 0, f"{model.__name__} must remain empty after Phase 6 operations"
