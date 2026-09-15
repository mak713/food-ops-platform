"""Payment API integration tests (Phase 6 Final Plan §G/§J; Final
Pre-Implementation Amendment §10)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models.order import Order, Payment
from tests.integration.api.helpers import csrf_headers, signup


def _create_order_with_total(client, amount: str) -> dict:
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    return client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": product["id"],
                    "underlying_quantity": "1",
                    "charged_unit_price": amount,
                }
            ]
        },
        headers=csrf_headers(client),
    ).json()


def _pay(client, order_id, amount, **overrides):
    payload = {"amount": amount, "payment_method": "cash", "payment_date": "2026-01-01"}
    payload.update(overrides)
    return client.post(
        f"/api/v1/orders/{order_id}/payments", json=payload, headers=csrf_headers(client)
    )


def test_payment_status_unpaid_partial_paid_and_overpaid_flow(client):
    order = _create_order_with_total(client, "100.00")

    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["payment_status"] == "UNPAID"
    assert detail["payments_total"] == "0.00"
    assert detail["overpayment_amount"] is None

    partial = _pay(client, order["id"], "40.00")
    assert partial.status_code == 201, partial.text
    assert partial.json()["payment_status"] == "PARTIALLY_PAID"
    assert partial.json()["payments_total"] == "40.00"

    exact = _pay(client, order["id"], "60.00")
    assert exact.status_code == 201, exact.text
    assert exact.json()["payment_status"] == "PAID"
    assert exact.json()["overpayment_amount"] is None

    overpay_attempt = _pay(client, order["id"], "10.00")
    assert overpay_attempt.status_code == 422
    assert overpay_attempt.json()["error"]["code"] == "PAYMENT_OVERAGE_WARNING"
    assert overpay_attempt.json()["error"]["issues"][0]["code"] == "PAYMENT_WOULD_OVERPAY"

    acknowledged = _pay(client, order["id"], "10.00", confirm_overpayment=True)
    assert acknowledged.status_code == 201, acknowledged.text
    body = acknowledged.json()
    assert body["payment_status"] == "PAID"
    assert body["payments_total"] == "110.00"
    assert body["overpayment_amount"] == "10.00"


def test_zero_dollar_order_with_zero_payments_is_unpaid(client):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()
    detail = client.get(f"/api/v1/orders/{created['id']}").json()
    assert detail["final_total"] == "0.00"
    assert detail["payment_status"] == "UNPAID"


def test_positive_payment_against_zero_dollar_order_requires_acknowledgment(client):
    signup(client)
    created = client.post("/api/v1/orders", json={}, headers=csrf_headers(client)).json()

    unacknowledged = _pay(client, created["id"], "5.00")
    assert unacknowledged.status_code == 422
    assert unacknowledged.json()["error"]["code"] == "PAYMENT_OVERAGE_WARNING"

    acknowledged = _pay(client, created["id"], "5.00", confirm_overpayment=True)
    assert acknowledged.status_code == 201, acknowledged.text
    body = acknowledged.json()
    assert body["payment_status"] == "PAID"
    assert body["overpayment_amount"] == "5.00"


def test_unacknowledged_overpayment_inserts_nothing(client, session):
    order = _create_order_with_total(client, "50.00")
    _pay(client, order["id"], "100.00")

    count = session.scalar(
        select(func.count()).select_from(Payment).where(Payment.order_id == order["id"])
    )
    assert count == 0


def test_no_new_payment_on_canceled_order(client, session):
    order = _create_order_with_total(client, "50.00")
    order_row = session.execute(select(Order).where(Order.id == order["id"])).scalar_one()
    order_row.status = "CANCELED"
    session.commit()

    response = _pay(client, order["id"], "10.00")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_CANCELED_NO_NEW_PAYMENTS"


def test_draft_total_reduction_below_recorded_payments_requires_acknowledgment(client):
    order = _create_order_with_total(client, "100.00")
    _pay(client, order["id"], "80.00")
    order_after_payment = client.get(f"/api/v1/orders/{order['id']}").json()

    original_line = order_after_payment["lines"][0]
    lowered = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={
            "version": order_after_payment["version"],
            "lines": [
                {
                    "id": original_line["id"],
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": original_line["product_id"],
                    "underlying_quantity": original_line["underlying_quantity"],
                    "charged_unit_price": "50.00",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert lowered.status_code == 422
    assert lowered.json()["error"]["code"] == "PAYMENT_OVERAGE_WARNING"

    acknowledged = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={
            "version": order_after_payment["version"],
            "confirm_overpayment": True,
            "lines": [
                {
                    "id": original_line["id"],
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": original_line["product_id"],
                    "underlying_quantity": original_line["underlying_quantity"],
                    "charged_unit_price": "50.00",
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["final_total"] == "50.00"
    assert acknowledged.json()["payments_total"] == "80.00"


def test_payments_have_no_edit_or_delete_endpoint(client):
    order = _create_order_with_total(client, "50.00")
    payment = _pay(client, order["id"], "10.00").json()
    payment_id = payment["payments"][0]["id"]

    patch_response = client.patch(
        f"/api/v1/orders/{order['id']}/payments/{payment_id}",
        json={"amount": "5.00"},
        headers=csrf_headers(client),
    )
    assert patch_response.status_code in (404, 405)

    delete_response = client.delete(
        f"/api/v1/orders/{order['id']}/payments/{payment_id}", headers=csrf_headers(client)
    )
    assert delete_response.status_code in (404, 405)


def test_payment_no_client_version_field_required(client):
    """Final Pre-Implementation Amendment §19: no `order_version` field is requested from
    the client for Payment creation — the row lock alone is the concurrency mechanism."""
    order = _create_order_with_total(client, "50.00")
    response = _pay(client, order["id"], "10.00")
    assert response.status_code == 201, response.text


# --- Checkpoint-3 correction 15: Payment cross-tenant isolation ------------------------


def test_foreign_tenant_cannot_add_payment_to_others_order(client):
    """A Business must not be able to add a Payment to another Business's Order — the
    existing non-revealing Order-not-found behavior (ADR-099) applies identically here."""
    order = _create_order_with_total(client, "50.00")
    order_id = order["id"]

    client.cookies.clear()
    signup(client, email="payment-tenant-b@example.com")

    response = _pay(client, order_id, "10.00")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
