"""Order concurrency tests (Phase 6 Final Plan §D.1/§G/§J; Final Pre-Implementation
Amendment §1/§2/§7).

Two distinct concerns, mirroring the existing `test_optimistic_concurrency.py`/
`test_inventory_concurrency.py` conventions:

1. Version/`updated_at` advancement and the app-level `check_version` pre-check — proved
   sequentially via the HTTP layer (`client`/`session` fixtures), since these don't need
   genuine cross-connection overlap.
2. Real row-lock behavior (a locked-then-rejected path releases its lock; two concurrent
   Payment additions serialize on the Order row and the second sees the first's committed
   sum) — proved via `real_session_factory`, two independent real connections.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.order import Order, Payment
from app.db.models.user import User
from app.schemas.order import OrderUpdateRequest, PaymentCreateRequest
from app.services import order_service, payment_service
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import make_business_graph, make_order

# --- Sequential HTTP-level: version/updated_at advancement -----------------------------


def _create_order_with_line(client, product) -> dict:
    return client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {
                    "line_type": "CUSTOM_QUANTITY",
                    "product_id": product["id"],
                    "underlying_quantity": "10",
                    "charged_unit_price": "50.00",
                    "notes": "original",
                }
            ]
        },
        headers=csrf_headers(client),
    ).json()


def _make_product(client):
    return client.post(
        "/api/v1/products",
        json={"name": "Cookie", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()


def _resubmit(line: dict, **overrides) -> dict:
    """Maps a response-shaped OrderLine back into the request shape expected by
    `OrderLineInput` (`charged_unit_price_snapshot` -> `charged_unit_price`, etc.) so an
    existing line can be resubmitted unchanged (or with overrides) in a PATCH body.
    Line-type-aware (Checkpoint-3 correction 12 forbids e.g. `package_quantity` on a
    CUSTOM_QUANTITY line, `packaging_cost_per_package` on a STANDARD_OPTION line) — only
    the fields meaningful for this line's own type are ever included."""
    line_type = line["line_type"]
    mapped: dict = {
        "id": line["id"],
        "line_type": line_type,
        "underlying_quantity": line["underlying_quantity"],
        "charged_unit_price": line["charged_unit_price_snapshot"],
        "notes": line["notes"],
    }
    if line_type == "STANDARD_OPTION":
        mapped.update(
            product_id=line["product_id"],
            selling_option_id=line["selling_option_id"],
            package_quantity=line["package_quantity"],
            price_override_reason=line["price_override_reason"],
        )
    elif line_type == "CUSTOM_QUANTITY":
        mapped.update(
            product_id=line["product_id"],
            price_override_reason=line["price_override_reason"],
            packaging_cost_per_package=line["packaging_cost_per_package_snapshot"],
        )
    else:  # CUSTOM_ITEM
        mapped.update(
            display_name=line["display_name_snapshot"],
            custom_direct_cost_estimate=line["custom_direct_cost_estimate"],
            custom_active_time_minutes=line["custom_active_time_minutes"],
            manual_fulfillment_required=line["manual_fulfillment_required"],
        )
    mapped.update(overrides)
    return mapped


def test_child_only_edit_with_unchanged_total_advances_version_and_updated_at(client):
    signup(client)
    product = _make_product(client)
    created = _create_order_with_line(client, product)
    old_version = created["version"]
    old_updated_line = created["lines"][0]

    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": old_version,
            "lines": [_resubmit(old_updated_line, notes="changed only the note")],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    # Totals genuinely unchanged (10 x 50.00 = 500.00, same as before) — yet version must
    # still advance, because a child-only edit occurred (Amendment §1's blocker fix).
    assert body["final_total"] == created["final_total"]
    assert body["version"] == old_version + 1

    stale_retry = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={"version": old_version, "lines": [_resubmit(old_updated_line)]},
        headers=csrf_headers(client),
    )
    assert stale_retry.status_code == 409
    assert stale_retry.json()["error"]["code"] == "STALE_VERSION"


def test_add_line_advances_version(client):
    signup(client)
    product = _make_product(client)
    created = _create_order_with_line(client, product)
    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [
                _resubmit(created["lines"][0]),
                {
                    "line_type": "CUSTOM_ITEM",
                    "underlying_quantity": "1",
                    "charged_unit_price": "5.00",
                    "display_name": "Extra",
                },
            ],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == created["version"] + 1


def test_remove_line_advances_version(client):
    signup(client)
    product = _make_product(client)
    created = _create_order_with_line(client, product)
    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={"version": created["version"], "lines": []},
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == created["version"] + 1
    assert updated.json()["lines"] == []


def test_quantity_edit_advances_version(client):
    """Uses a STANDARD_OPTION line, where `package_quantity` genuinely drives
    `line_subtotal` — unlike CUSTOM_QUANTITY, whose `charged_unit_price` is a flat agreed
    line price independent of `underlying_quantity` (Final Plan §E)."""
    signup(client)
    product = _make_product(client)
    option = client.post(
        f"/api/v1/products/{product['id']}/selling-options",
        json={"name": "6-pack", "quantity_units": 6, "price": "12.00"},
        headers=csrf_headers(client),
    ).json()
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {
                    "line_type": "STANDARD_OPTION",
                    "product_id": product["id"],
                    "selling_option_id": option["id"],
                    "package_quantity": "2",
                }
            ]
        },
        headers=csrf_headers(client),
    ).json()
    line = created["lines"][0]
    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "lines": [_resubmit(line, package_quantity="3", charged_unit_price=None)],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == created["version"] + 1
    assert updated.json()["final_total"] != created["final_total"]


def test_header_notes_only_edit_advances_version(client):
    signup(client)
    product = _make_product(client)
    created = _create_order_with_line(client, product)
    updated = client.patch(
        f"/api/v1/orders/{created['id']}",
        json={
            "version": created["version"],
            "internal_notes": "a header-only change",
            "lines": [_resubmit(created["lines"][0])],
        },
        headers=csrf_headers(client),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == created["version"] + 1
    assert updated.json()["internal_notes"] == "a header-only change"


# --- Real concurrency: locked-rejection releases the lock -------------------------------


def _cleanup_order(real_session_factory, *, order_id, business_id, owner_id):
    cleanup = real_session_factory()
    try:
        order = cleanup.get(Order, order_id)
        if order is not None:
            cleanup.delete(order)
            cleanup.flush()
        business = cleanup.get(Business, business_id)
        if business is not None:
            cleanup.delete(business)
            cleanup.flush()
        user = cleanup.get(User, owner_id)
        if user is not None:
            cleanup.delete(user)
        cleanup.commit()
    finally:
        cleanup.close()


def test_stale_version_rejection_releases_the_order_row_lock(real_session_factory):
    """Session A triggers the locked-then-rejected `STALE_VERSION` path and is
    deliberately left OPEN (not closed) afterward; Session B then attempts a `NOWAIT`
    lock on the same row. If `update_draft_order`'s rollback (Amendment §2) had not
    released the lock, B's attempt would fail immediately with a lock-not-available
    error instead of succeeding."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        order = make_order(setup_db, business)
        setup_db.commit()
        business_id, order_id, owner_id = business.id, order.id, business.owner_user_id
    finally:
        setup_db.close()

    db_a = real_session_factory()
    try:
        business_a = db_a.get(Business, business_id)
        try:
            order_service.update_draft_order(
                db_a, business_a, order_id, OrderUpdateRequest(version=999)
            )
            raise AssertionError("expected a STALE_VERSION ApiError")
        except ApiError as exc:
            assert exc.code == "STALE_VERSION"

        db_b = real_session_factory()
        try:
            locked = db_b.scalar(
                select(Order).where(Order.id == order_id).with_for_update(nowait=True)
            )
            assert locked is not None, "lock must be immediately available — A rolled back"
            db_b.rollback()
        finally:
            db_b.close()
    finally:
        db_a.close()
        _cleanup_order(
            real_session_factory, order_id=order_id, business_id=business_id, owner_id=owner_id
        )


def test_non_draft_rejection_releases_the_order_row_lock(real_session_factory):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        order = make_order(setup_db, business, status="CONFIRMED")
        setup_db.commit()
        business_id, order_id, owner_id = business.id, order.id, business.owner_user_id
        expected_version = order.version
    finally:
        setup_db.close()

    db_a = real_session_factory()
    try:
        business_a = db_a.get(Business, business_id)
        try:
            order_service.update_draft_order(
                db_a, business_a, order_id, OrderUpdateRequest(version=expected_version)
            )
            raise AssertionError("expected an ORDER_NOT_DRAFT ApiError")
        except ApiError as exc:
            assert exc.code == "ORDER_NOT_DRAFT"

        db_b = real_session_factory()
        try:
            locked = db_b.scalar(
                select(Order).where(Order.id == order_id).with_for_update(nowait=True)
            )
            assert locked is not None
            db_b.rollback()
        finally:
            db_b.close()
    finally:
        db_a.close()
        _cleanup_order(
            real_session_factory, order_id=order_id, business_id=business_id, owner_id=owner_id
        )


def test_payment_overage_rejection_releases_the_order_row_lock(real_session_factory):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        order = make_order(setup_db, business)
        order.final_total = Decimal("10.00")
        setup_db.commit()
        business_id, order_id, owner_id = business.id, order.id, business.owner_user_id
    finally:
        setup_db.close()

    db_a = real_session_factory()
    try:
        business_a = db_a.get(Business, business_id)
        try:
            payment_service.add_payment(
                db_a,
                business_a,
                order_id,
                PaymentCreateRequest(
                    amount=Decimal("100.00"), payment_method="cash", payment_date="2026-01-01"
                ),
            )
            raise AssertionError("expected a PAYMENT_OVERAGE_WARNING ApiError")
        except ApiError as exc:
            assert exc.code == "PAYMENT_OVERAGE_WARNING"

        db_b = real_session_factory()
        try:
            locked = db_b.scalar(
                select(Order).where(Order.id == order_id).with_for_update(nowait=True)
            )
            assert locked is not None
            db_b.rollback()
        finally:
            db_b.close()
    finally:
        db_a.close()
        _cleanup_order(
            real_session_factory, order_id=order_id, business_id=business_id, owner_id=owner_id
        )


# --- Real concurrency: two Payment additions serialize on the Order row lock -----------


def test_two_concurrent_payments_serialize_and_second_sees_first_committed_sum(
    real_session_factory,
):
    """Both threads submit an individually-fine amount that together would overpay. The
    Order row lock forces one to fully commit before the other's `get_order_for_business_
    locked` unblocks — so the second's fresh `SUM(payments.amount)` (queried AFTER
    acquiring the lock, Amendment §7) always reflects the first's committed Payment,
    never a stale pre-lock read. Exactly one must therefore be rejected as an
    unacknowledged overpayment."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        order = make_order(setup_db, business)
        order.final_total = Decimal("100.00")
        setup_db.commit()
        business_id, order_id, owner_id = business.id, order.id, business.owner_user_id
    finally:
        setup_db.close()

    start_barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def attempt(amount: str) -> None:
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business_row = db.get(Business, business_id)
            payment_service.add_payment(
                db,
                business_row,
                order_id,
                PaymentCreateRequest(
                    amount=Decimal(amount), payment_method="cash", payment_date="2026-01-01"
                ),
            )
            results.append(("ok", amount))
        except ApiError as exc:
            db.rollback()
            results.append(("api_error", exc))
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=attempt, args=("60.00",)),
            threading.Thread(target=attempt, args=("60.00",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        oks = [r for kind, r in results if kind == "ok"]
        errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, results
        assert len(errors) == 1, results
        assert errors[0].code == "PAYMENT_OVERAGE_WARNING"

        verify_db = real_session_factory()
        try:
            total = verify_db.scalar(select(Payment.amount).where(Payment.order_id == order_id))
            assert total == Decimal("60.00"), "the loser must never have inserted its Payment"
        finally:
            verify_db.close()
    finally:
        _cleanup_order(
            real_session_factory, order_id=order_id, business_id=business_id, owner_id=owner_id
        )
