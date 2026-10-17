"""Customers CSV must derive its Customer rows and its Guest-summary row from ONE database
statement (ADR-158, Phase 10 remediation Finding 3).

Under READ COMMITTED every statement takes its own snapshot, so an export built from two
statements can describe two different database states if an Order is completed in between.
This test is barrier-controlled: a SQLAlchemy `after_cursor_execute` hook on the reader's own
connection fires as soon as the *first* statement that reads `orders` has executed, and — before
the reader is allowed to continue — a second, real connection commits a newly Completed Order
and the hook waits for that commit. Any implementation that issues a second statement afterwards
(the former Guest-summary-then-Customers design) sees the new Order in one half of the CSV only.

Consistency invariant observable *inside the CSV*: the Guest row's
`share_of_completed_orders = guest_orders / completed_total`, and in a consistent state
`completed_total = SUM(customer rows' completed_orders) + guest_orders` (every Completed Order
here is either a valid Customer's or a Guest's).
"""

from __future__ import annotations

import csv
import io
import threading
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app.api import deps
from app.api.v1 import analytics as analytics_router
from app.db.enums import CostType, FulfillmentMethod
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from tests.integration.api.analytics_support import Builder, at, custom_qty
from tests.integration.schema.factories import make_business_graph


def _cleanup(real_session_factory, *, business_id):
    cleanup = real_session_factory()
    try:
        business = cleanup.get(Business, business_id)
        if business is not None:
            owner_id = business.owner_user_id
            cleanup.delete(business)
            cleanup.flush()
            user = cleanup.get(User, owner_id)
            if user is not None:
                cleanup.delete(user)
        cleanup.commit()
    finally:
        cleanup.close()


@pytest.fixture()
def frozen_clock(monkeypatch):
    monkeypatch.setattr(
        analytics_router, "_utc_now", lambda: datetime(2026, 10, 16, 18, tzinfo=UTC)
    )


def test_customers_csv_rows_and_guest_summary_come_from_one_snapshot(
    real_session_factory, frozen_clock
):
    setup = real_session_factory()
    try:
        business = make_business_graph(setup)
        builder = Builder(setup, business)
        alice = builder.customer("Alice")
        product = builder.product("Cookie")
        for i in range(3):
            builder.order(
                completed_at=at(5 + i, 15),
                customer=alice,
                method=FulfillmentMethod.PICKUP,
                number=f"C-{i}",
                lines=[custom_qty(product, "2", "10.00", {CostType.PACKAGING: "1.00"})],
            )
            builder.order(
                completed_at=at(8 + i, 15),
                customer=None,
                number=f"G-{i}",
                lines=[custom_qty(product, "1", "5.00", {CostType.PACKAGING: "0.50"})],
            )
        setup.commit()
        business_id, alice_id, product_id = business.id, alice.id, product.id
    finally:
        setup.close()

    reader = real_session_factory()
    fired = threading.Event()
    writer_errors: list[Exception] = []

    def _writer() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            b = Builder(db, biz)
            customer = db.get(Customer, alice_id)
            from app.db.models.product import Product

            prod = db.get(Product, product_id)
            b.order(
                completed_at=at(20, 12),
                customer=customer,
                number="C-LATE",
                lines=[custom_qty(prod, "4", "40.00", {CostType.PACKAGING: "2.00"})],
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001
            writer_errors.append(exc)
        finally:
            db.close()

    reader_conn = reader.connection()

    def _barrier(conn, cursor, statement, parameters, context, executemany):
        # First statement that reads `orders` on the reader's connection = the analytics query.
        if conn is reader_conn and not fired.is_set() and "FROM orders" in statement:
            fired.set()
            thread = threading.Thread(target=_writer)
            thread.start()
            thread.join(timeout=30)  # the concurrent Completion is committed before we continue

    engine = reader.get_bind()
    event.listen(engine, "after_cursor_execute", _barrier)
    business_row = reader.get(Business, business_id)
    app.dependency_overrides[get_db] = lambda: reader
    app.dependency_overrides[deps.get_current_business] = lambda: business_row
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/analytics/customers.csv",
                params={"range": "CUSTOM", "start_date": "2026-10-01", "end_date": "2026-10-31"},
            )
        assert response.status_code == 200, response.text
        assert fired.is_set(), "the barrier never fired: the export issued no `orders` statement"
        assert not writer_errors, writer_errors
    finally:
        event.remove(engine, "after_cursor_execute", _barrier)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(deps.get_current_business, None)
        reader.rollback()
        reader.close()
        # the concurrent Completion really was committed by the other connection
        check = real_session_factory()
        try:
            late = check.execute(
                text(
                    "SELECT count(*) FROM orders WHERE business_id = :b AND order_number = 'C-LATE'"
                ),
                {"b": business_id},
            ).scalar_one()
        finally:
            check.close()
        _cleanup(real_session_factory, business_id=business_id)
    assert late == 1

    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))
    customers = [r for r in rows if r["row_type"] == "CUSTOMER"]
    guest = next(r for r in rows if r["row_type"] == "GUEST_SUMMARY")
    guest_orders = int(guest["completed_orders"])
    customer_orders = sum(int(r["completed_orders"]) for r in customers)
    implied_total = customer_orders + guest_orders
    expected_share = (Decimal(guest_orders) / Decimal(implied_total)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    assert Decimal(guest["share_of_completed_orders"]) == expected_share, (
        "the Guest summary and the Customer rows describe different database states: "
        f"guest share {guest['share_of_completed_orders']} vs rows imply {expected_share}"
    )
