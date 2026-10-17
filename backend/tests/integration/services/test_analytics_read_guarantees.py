"""Phase 10 read-side guarantees (extends ADR-144 / Phase 9 X2, X3): Analytics

* writes nothing (whole-row fingerprints of every domain table unchanged),
* leaves the session with no pending changes,
* takes no workflow row lock and never blocks on one (real, separate connections), and
* stays valid while another connection commits Completed Orders concurrently.

Real committed data is used for the lock/concurrency tests — the savepoint-wrapped `session`
fixture cannot demonstrate cross-connection behavior.
"""

from __future__ import annotations

import threading
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.db.base import Base
from app.db.enums import CostType, FulfillmentMethod
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation
from app.db.models.customer import Customer
from app.db.models.order import Order, OrderLine, Payment
from app.db.models.product import Product
from app.db.models.user import User
from app.domain.analytics_periods import AnalyticsRange, resolve_periods
from app.services import analytics_service as svc
from tests.integration.api.analytics_support import Builder, at, custom_qty
from tests.integration.schema.factories import make_business_graph

_INFRASTRUCTURE_TABLES = {"sessions", "password_reset_tokens", "users"}


def _periods(business, *, compare=True):
    periods, issues = resolve_periods(
        range_=AnalyticsRange.CUSTOM,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        business_today=date(2026, 10, 16),
        tz=ZoneInfo(business.timezone),
        compare=compare,
    )
    assert not issues
    return periods


def _world(db):
    business = make_business_graph(db)
    b = Builder(db, business)
    alice = b.customer("Alice")
    cookie = b.product("Cookie")
    for i in range(6):
        b.order(
            completed_at=at(5 + i, 15),
            customer=alice if i % 2 else None,
            method=FulfillmentMethod.PICKUP,
            number=f"R-{i}",
            payments=["1.00"],
            lines=[custom_qty(cookie, "4", "10.00", {CostType.PACKAGING: "1.00"})],
        )
    db.commit()
    return business


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


def _fingerprint(db) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _INFRASTRUCTURE_TABLES:
            continue
        row = db.execute(
            text(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, ',' ORDER BY t::text)), '') "
                f"FROM {table.name} t"
            )
        ).one()
        result[table.name] = (row[0], row[1])
    return result


def _all_reads(db, business):
    periods = _periods(business)
    current = periods.current
    svc.get_overview(db, business, periods)
    svc.get_trends(db, business, current, ZoneInfo(business.timezone))
    svc.get_products(db, business, current)
    svc.get_customers(db, business, current, limit=50, offset=0)
    svc.get_orders(db, business, current, limit=50, offset=0)
    list(
        svc.stream_customer_export(
            db, business, current, sort=svc.CustomerSort.REVENUE, direction=None
        )
    )
    list(
        svc.stream_orders(
            db,
            business,
            current,
            status=svc.OrderStatusFilter.ALL,
            outstanding_only=False,
            sort=svc.OrderSort.REPORTED_AT,
            direction=None,
        )
    )


def test_reads_write_nothing_to_any_domain_table(real_session_factory):
    setup = real_session_factory()
    try:
        business_id = _world(setup).id
    finally:
        setup.close()
    try:
        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            before = _fingerprint(db)
            _all_reads(db, business)
            db.commit()  # would persist anything a read had (wrongly) staged
            after = _fingerprint(db)
        finally:
            db.close()
        assert before == after
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_reads_leave_the_session_with_no_pending_changes(real_session_factory):
    setup = real_session_factory()
    try:
        business_id = _world(setup).id
    finally:
        setup.close()
    try:
        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            _all_reads(db, business)
            assert not db.new and not db.dirty and not db.deleted
        finally:
            db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_reads_never_block_on_workflow_row_locks(real_session_factory):
    """Another connection holds `FOR UPDATE` on every Order, Line, ledger, Payment, Customer
    and Product row of the tenant. A short `lock_timeout` makes any read that tried to lock one
    fail fast, so completing at all proves the Analytics reads are lock-free."""
    setup = real_session_factory()
    try:
        business_id = _world(setup).id
    finally:
        setup.close()

    holder = real_session_factory()
    try:
        for model in (Order, OrderLine, OrderCostAllocation, Payment, Customer, Product):
            holder.execute(select(model).where(model.business_id == business_id).with_for_update())
        reader = real_session_factory()
        try:
            reader.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            business = reader.get(Business, business_id)
            try:
                _all_reads(reader, business)
            except OperationalError as exc:  # pragma: no cover - only on regression
                raise AssertionError(f"an Analytics read blocked on a row lock: {exc}") from exc
        finally:
            reader.rollback()
            reader.close()
    finally:
        holder.rollback()
        holder.close()
        _cleanup(real_session_factory, business_id=business_id)


def test_reads_stay_valid_while_other_connections_commit_orders(real_session_factory):
    setup = real_session_factory()
    try:
        business_id = _world(setup).id
    finally:
        setup.close()

    errors: list[Exception] = []
    stop = threading.Event()

    def _writer() -> None:
        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            builder = Builder(db, business)
            product = builder.product("Writer Cookie")
            for i in range(12):
                builder.order(
                    completed_at=at(20, 12, i),
                    number=f"W-{i}",
                    lines=[custom_qty(product, "1", "5.00", {CostType.PACKAGING: "0.50"})],
                )
                db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()
            db.close()

    thread = threading.Thread(target=_writer)
    try:
        thread.start()
        reads = 0
        while not stop.is_set() or reads < 3:
            db = real_session_factory()
            try:
                business = db.get(Business, business_id)
                periods = _periods(business)
                svc.get_overview(db, business, periods)
                products = svc.get_products(db, business, periods.current)
                # Each response is internally coherent: one statement, one fact set.
                assert products.order_level.unreconciled_revenue == 0
                assert products.totals.revenue == (
                    sum((r.revenue for r in products.rows), start=products.totals.revenue * 0)
                    + (products.custom_items.revenue if products.custom_items else 0)
                    + products.order_level.order_adjustments
                    + products.order_level.manual_tax
                )
                trends = svc.get_trends(db, business, periods.current, ZoneInfo(business.timezone))
                assert trends.totals.revenue == sum(
                    (b.revenue for b in trends.buckets), start=trends.totals.revenue * 0
                )
            finally:
                db.close()
            reads += 1
        thread.join()
        assert not errors, errors
    finally:
        thread.join()
        _cleanup(real_session_factory, business_id=business_id)
