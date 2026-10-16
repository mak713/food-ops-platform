"""Phase 9 read-side guarantees (Acceptance Matrix X2/X3/X5): the Dashboard, Shopping List,
and the new Production Plan / inventory read derivations

* write nothing (zero-write, whole-row fingerprints of every domain table),
* take no workflow row lock and never block on one (real, separate connections), and
* stay valid and error-free while a real Finish Production commits concurrently.

Real committed data is used for the lock/concurrency tests (the savepoint-wrapped
`session` fixture cannot demonstrate cross-connection behavior).
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.db.base import Base
from app.db.enums import OrderLineType, OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order
from app.db.models.product import Product
from app.db.models.production import ProductionRequirement
from app.db.models.surplus import SurplusInventory
from app.db.models.user import User
from app.services import dashboard_service, shopping_list_service
from app.services.operational_recalculation_service import recalculate_product_closure
from app.services.production_run_service import finish_production, start_production
from app.services.requirement_insight_service import build_requirement_insights
from tests.integration.schema.factories import (
    make_business_graph,
    make_ingredient,
    make_order,
    make_order_line,
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
    make_surplus_inventory,
)

_TODAY = date(2026, 6, 1)
_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)

_INFRASTRUCTURE_TABLES = {"sessions", "password_reset_tokens"}


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


def _world(db, *, demand="12", physical="10000"):
    business = make_business_graph(db)
    product = make_product(db, business, product_type=ProductType.PRODUCED, name="Cookie")
    recipe = make_recipe(db, business, product)
    revision = make_recipe_revision(db, business, recipe, yield_quantity=Decimal("12"))
    revision.elapsed_time_minutes = 120
    ingredient = make_ingredient(db, business, name="Flour")
    ingredient.physical_quantity = Decimal(physical)
    ingredient.weighted_average_unit_cost = Decimal("0.02")
    make_recipe_revision_ingredient(db, business, revision, ingredient, quantity=Decimal("500"))
    lot = make_surplus_inventory(db, business, product, physical_quantity=3)
    lot.usable_through_date = _TODAY + timedelta(days=1)
    order = make_order(db, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = _TODAY
    order.fulfillment_time = time(17, 0)
    make_order_line(
        db,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        underlying_quantity=Decimal(demand),
    )
    db.flush()
    recalculate_product_closure(db, business, product, business_today=_TODAY)
    db.commit()
    return business, product, ingredient, order


def _fingerprint(db, business_id) -> dict[str, tuple[int, str]]:
    """Whole-row fingerprint of every domain table (excluding auth infrastructure)."""
    result: dict[str, tuple[int, str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _INFRASTRUCTURE_TABLES or table.name == "users":
            continue
        name = table.name
        row = db.execute(
            text(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, ',' ORDER BY t::text)), '') "
                f"FROM {name} t"
            )
        ).one()
        result[name] = (row[0], row[1])
    return result


def _all_reads(db, business):
    dashboard_service.build_dashboard(db, business, now_utc=_NOW, business_today=_TODAY)
    shopping_list_service.get_shopping_list(db, business, business_today=_TODAY)
    shopping_list_service.get_shopping_list(
        db,
        business,
        business_today=_TODAY,
        start_date=_TODAY + timedelta(days=1),
        end_date=_TODAY + timedelta(days=5),
    )
    requirements = db.scalars(select(ProductionRequirement)).all()
    build_requirement_insights(db, business, requirements)


# ------------------------------------------------------------------------------ X2


def test_reads_write_nothing_to_any_domain_table(real_session_factory):
    setup = real_session_factory()
    try:
        business, *_ = _world(setup)
        business_id = business.id
    finally:
        setup.close()

    try:
        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            before = _fingerprint(db, business_id)
            _all_reads(db, business)
            db.commit()  # would persist anything a read had (wrongly) staged
            after_commit = _fingerprint(db, business_id)
        finally:
            db.close()
        assert before == after_commit
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_read_services_leave_the_session_with_no_pending_changes(real_session_factory):
    setup = real_session_factory()
    try:
        business, *_ = _world(setup)
        business_id = business.id
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


# ------------------------------------------------------------------------------ X3


def test_reads_never_block_on_workflow_row_locks(real_session_factory):
    """Another connection holds `FOR UPDATE` on the Order, Product, Ingredient, Surplus
    lot and ProductionRequirement rows (exactly the rows Phase 7/8 workflows lock). A
    `lock_timeout` makes any read that tried to lock or write one of them fail fast
    instead of hanging — so completing at all proves the reads are lock-free."""
    setup = real_session_factory()
    try:
        business, product, ingredient, order = _world(setup)
        business_id, product_id = business.id, product.id
        ingredient_id, order_id = ingredient.id, order.id
    finally:
        setup.close()

    holder = real_session_factory()
    try:
        holder.execute(select(Order).where(Order.id == order_id).with_for_update())
        holder.execute(select(Product).where(Product.id == product_id).with_for_update())
        holder.execute(select(Ingredient).where(Ingredient.id == ingredient_id).with_for_update())
        holder.execute(
            select(SurplusInventory)
            .where(SurplusInventory.business_id == business_id)
            .with_for_update()
        )
        holder.execute(
            select(ProductionRequirement)
            .where(ProductionRequirement.business_id == business_id)
            .with_for_update()
        )

        reader = real_session_factory()
        try:
            reader.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            business = reader.get(Business, business_id)
            try:
                _all_reads(reader, business)
            except OperationalError as exc:  # pragma: no cover - only on regression
                raise AssertionError(f"a read blocked on a workflow row lock: {exc}") from exc
        finally:
            reader.rollback()
            reader.close()
    finally:
        holder.rollback()
        holder.close()
        _cleanup(real_session_factory, business_id=business_id)


# ------------------------------------------------------------------------------ X5


def test_reads_stay_valid_while_finish_production_commits_concurrently(real_session_factory):
    setup = real_session_factory()
    try:
        business, product, ingredient, order = _world(setup, demand="6")
        requirement_id = setup.scalars(
            select(ProductionRequirement.id).where(ProductionRequirement.product_id == product.id)
        ).first()
        run = start_production(
            setup, business, requirement_id, batches=None, acknowledged_warning_fingerprint=None
        )
        setup.commit()
        business_id, run_id = business.id, run.id
    finally:
        setup.close()

    errors: list[Exception] = []
    snapshots: list = []
    finished = threading.Event()

    def _finisher() -> None:
        db = real_session_factory()
        try:
            biz = db.get(Business, business_id)
            finish_production(
                db,
                biz,
                run_id,
                expected_version=1,
                actual_output_quantity=Decimal("6"),
                ingredient_actual_quantities=None,
                business_today=_TODAY,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()
            finished.set()

    thread = threading.Thread(target=_finisher)
    try:
        thread.start()
        reads = 0
        while (not finished.is_set() or reads < 3) and reads < 200:
            db = real_session_factory()
            try:
                biz = db.get(Business, business_id)
                body = dashboard_service.build_dashboard(
                    db, biz, now_utc=_NOW, business_today=_TODAY
                )
                snapshots.append(body)
                shopping_list_service.get_shopping_list(db, biz, business_today=_TODAY)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()
            reads += 1
        thread.join(timeout=20)

        assert not errors, f"unexpected errors during concurrent read/finish: {errors}"
        assert snapshots
        for body in snapshots:
            snap = body.todays_snapshot
            # Internal consistency of every individual response.
            assert snap.orders_ready + snap.orders_awaiting == snap.orders_due_today
            assert snap.active_runs == len(body.active_production)
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_expected_schema_has_no_phase9_tables_or_columns():
    """Phase 9 is read-only: it introduced no table (guards against accidental schema
    creep alongside the explicit no-migration check run in the verification step)."""
    names = {t.name for t in Base.metadata.sorted_tables}
    assert not {n for n in names if "dashboard" in n or "alert" in n or "shopping" in n}
    assert "dashboard" not in {c.name for t in Base.metadata.sorted_tables for c in t.columns}
