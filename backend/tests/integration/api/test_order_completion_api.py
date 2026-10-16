"""Order Completion / Fulfillment API tests: lifecycle, Purchased and Surplus physical
consumption and history, the five-category historical-cost ledger (incl. known-zero vs
absent), packaging snapshot immutability, rejection with zero writes, stale version,
idempotency, production-lock, coverage-loss, tenant isolation, atomic rollback at every
step boundary, and Dashboard/payment consequences."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import CostType, OrderStatus
from app.db.models.cost import OrderCostAllocation
from app.db.models.order import Order, OrderStatusHistory
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.surplus import (
    PurchasedProductReservation,
    SurplusAllocation,
    SurplusInventory,
    SurplusTransaction,
)
from app.services import order_completion_service as ocs
from tests.integration.api.completion_support import (
    build_ready_order,
    domain_fingerprint,
    produced_product,
    purchased_product,
    reload,
    surplus_lot,
)
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.api.phase9_support import business_by_name, business_today
from tests.integration.schema.factories import (
    make_business_graph,
    make_payment,
    make_production_run,
)


def _setup(client, session: Session):
    signup(client, email=f"completion-{uuid.uuid4().hex[:10]}@example.com")
    return business_by_name(session)


def _complete(client, order: Order, *, version: int | None = None):
    return client.post(
        f"/api/v1/orders/{order.id}/complete",
        json={"version": order.version if version is None else version},
        headers=csrf_headers(client),
    )


def _ledger(session: Session, order: Order):
    session.expire_all()
    return list(
        session.scalars(
            select(OrderCostAllocation)
            .where(OrderCostAllocation.order_id == order.id)
            .order_by(OrderCostAllocation.cost_type, OrderCostAllocation.id)
        )
    )


def _by_type(rows, cost_type):
    return [r for r in rows if r.cost_type is cost_type]


# ============================================================ Purchased fulfillment


def test_purchased_completion_deducts_stock_snapshots_wac_and_writes_linked_ledger(
    client, session: Session
):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10", wac="1.50")
    order = build_ready_order(session, business, [{"product": juice, "qty": "4"}])
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    version_before = inventory.version
    before = datetime.now(UTC)

    response = _complete(client, order)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["completed_at"] is not None
    completed_at = datetime.fromisoformat(body["completed_at"])
    assert completed_at.tzinfo is not None and before <= completed_at <= datetime.now(UTC)
    assert body["ready_at"] is not None  # retained as lifecycle history

    session.expire_all()
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    assert inventory.physical_quantity == Decimal("6")
    assert inventory.weighted_average_unit_cost == Decimal("1.50")  # unchanged by fulfillment
    assert inventory.latest_purchase_unit_cost == Decimal("9.99")
    assert inventory.replacement_unit_cost == Decimal("8.88")
    assert inventory.version > version_before

    txn = session.scalars(
        select(PurchasedProductInventoryTransaction).where(
            PurchasedProductInventoryTransaction.order_id == order.id
        )
    ).one()
    assert txn.transaction_type.value == "ORDER_FULFILLMENT"
    assert txn.quantity_change == Decimal("-4")
    assert txn.unit_cost == Decimal("1.50")  # WAC, never latest/replacement
    assert txn.total_cost == Decimal("6.00")
    assert txn.order_line_id == order.lines[0].id

    purchased_rows = _by_type(_ledger(session, order), CostType.PURCHASED_GOOD)
    assert len(purchased_rows) == 1
    assert purchased_rows[0].amount == Decimal("6.00")
    assert purchased_rows[0].purchased_inventory_transaction_id == txn.id
    assert purchased_rows[0].order_line_id == order.lines[0].id

    # Reservations released; lifecycle history recorded.
    assert session.scalars(select(PurchasedProductReservation)).all() == []
    history = session.scalars(
        select(OrderStatusHistory).where(
            OrderStatusHistory.order_id == order.id,
            OrderStatusHistory.to_status == OrderStatus.COMPLETED,
        )
    ).one()
    assert history.from_status is OrderStatus.READY

    # Historical cost read model, from the ledger only.
    cost = body["historical_cost"]
    assert Decimal(cost["by_type"]["PURCHASED_GOOD"]) == Decimal("6.00")
    assert Decimal(cost["by_type"]["PRODUCTION"]) == 0
    assert Decimal(cost["total"]) == Decimal("6.00")


def test_completion_does_not_require_payment(client, session: Session):
    business = _setup(client, session)
    order = build_ready_order(
        session, business, [{"product": purchased_product(session, business), "qty": "1"}]
    )
    assert order.payments == []  # unpaid
    assert _complete(client, order).status_code == 200


def test_multiple_purchased_lines_for_one_product_aggregate_and_keep_provenance(
    client, session: Session
):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="7", wac="1.50")
    order = build_ready_order(
        session,
        business,
        [{"product": juice, "qty": "3"}, {"product": juice, "qty": "4"}],
    )
    response = _complete(client, order)
    assert response.status_code == 200, response.text

    session.expire_all()
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    assert inventory.physical_quantity == Decimal("0")  # exactly sufficient, never negative
    txns = session.scalars(
        select(PurchasedProductInventoryTransaction).where(
            PurchasedProductInventoryTransaction.order_id == order.id
        )
    ).all()
    assert sorted(t.quantity_change for t in txns) == [Decimal("-4"), Decimal("-3")]
    assert {t.order_line_id for t in txns} == {line.id for line in order.lines}
    rows = _by_type(_ledger(session, order), CostType.PURCHASED_GOOD)
    assert sorted(r.amount for r in rows) == [Decimal("4.50"), Decimal("6.00")]
    assert {r.purchased_inventory_transaction_id for r in rows} == {t.id for t in txns}


def test_zero_wac_writes_an_explicit_zero_purchased_row(client, session: Session):
    business = _setup(client, session)
    free = purchased_product(session, business, physical="5", wac="0")
    order = build_ready_order(session, business, [{"product": free, "qty": "5"}])
    assert _complete(client, order).status_code == 200
    rows = _by_type(_ledger(session, order), CostType.PURCHASED_GOOD)
    assert len(rows) == 1 and rows[0].amount == Decimal("0.00")


# ============================================================== Surplus fulfillment


def test_surplus_completion_consumes_exact_allocations_across_lots(client, session: Session):
    business = _setup(client, session)
    cookie = produced_product(session, business)
    lot_a = surplus_lot(session, business, cookie, physical="4", basis="0.500000")
    lot_b = surplus_lot(session, business, cookie, physical="6", basis="1.250000")
    # 8 units of demand are covered by the two lots; another Order competes for the rest.
    order = build_ready_order(session, business, [{"product": cookie, "qty": "8"}])
    allocations = session.scalars(select(SurplusAllocation)).all()
    assert sum(a.quantity for a in allocations) == Decimal("8")
    expected = {a.surplus_inventory_id: a.quantity for a in allocations}

    response = _complete(client, order)
    assert response.status_code == 200, response.text

    session.expire_all()
    lots = {lot.id: lot for lot in session.scalars(select(SurplusInventory))}
    assert lots[lot_a.id].physical_quantity == Decimal("4") - expected.get(lot_a.id, 0)
    assert lots[lot_b.id].physical_quantity == Decimal("6") - expected.get(lot_b.id, 0)
    assert session.scalars(select(SurplusAllocation)).all() == []  # consumed rows removed

    txns = session.scalars(
        select(SurplusTransaction).where(SurplusTransaction.order_id == order.id)
    ).all()
    assert len(txns) == len(allocations)
    assert all(t.transaction_type.value == "ORDER_FULFILLMENT" for t in txns)
    assert all(t.quantity_change < 0 and t.order_line_id == order.lines[0].id for t in txns)

    rows = _by_type(_ledger(session, order), CostType.SURPLUS)
    assert len(rows) == len(allocations)
    bases = {lot_a.id: Decimal("0.5"), lot_b.id: Decimal("1.25")}
    for row in rows:
        assert row.surplus_inventory_id in bases
        assert row.amount == (
            expected[row.surplus_inventory_id] * bases[row.surplus_inventory_id]
        ).quantize(Decimal("0.01"))
    # source_total_cost_basis is immutable provenance — never touched.
    assert all(lots[i].source_total_cost_basis is None for i in lots)


def test_zero_surplus_basis_writes_an_explicit_zero_row(client, session: Session):
    business = _setup(client, session)
    cookie = produced_product(session, business)
    surplus_lot(session, business, cookie, physical="3", basis="0")
    order = build_ready_order(session, business, [{"product": cookie, "qty": "3"}])
    assert _complete(client, order).status_code == 200
    rows = _by_type(_ledger(session, order), CostType.SURPLUS)
    assert len(rows) == 1 and rows[0].amount == Decimal("0.00")


def test_completion_leaves_a_siblings_surplus_allocation_intact(client, session: Session):
    business = _setup(client, session)
    cookie = produced_product(session, business)
    surplus_lot(session, business, cookie, physical="10", basis="0.40")
    ready = build_ready_order(session, business, [{"product": cookie, "qty": "4"}])
    sibling = build_ready_order(
        session, business, [{"product": cookie, "qty": "3"}], order_number="ORD-SIB"
    )
    assert _complete(client, ready).status_code == 200
    session.expire_all()
    remaining = session.scalars(select(SurplusAllocation)).all()
    assert sum(a.quantity for a in remaining) == Decimal("3")
    assert {a.order_id for a in remaining} == {sibling.id}
    lot = session.scalars(select(SurplusInventory)).one()
    assert lot.physical_quantity == Decimal("6")  # 10 - 4 consumed
    assert reload(session, Order, sibling.id).status is OrderStatus.READY


# ================================================ Packaging / Custom Direct / PRODUCTION


def test_packaging_and_custom_direct_use_snapshots_including_known_zero(client, session: Session):
    business = _setup(client, session)
    free = purchased_product(session, business, physical="9", wac="1")
    order = build_ready_order(
        session,
        business,
        [
            {"product": free, "qty": "2", "packaging": "1.10"},
            {"product": None, "qty": "1", "packaging": "0.00", "custom_estimate": "0.00"},
            {"product": None, "qty": "1", "packaging": "0.55", "custom_estimate": "7.25"},
            {"product": None, "qty": "1", "packaging": "0.00", "custom_estimate": None},
        ],
    )
    assert _complete(client, order).status_code == 200
    rows = _ledger(session, order)

    packaging = {r.order_line_id: r.amount for r in _by_type(rows, CostType.PACKAGING)}
    assert len(packaging) == 4  # a row per line, explicit 0.00 included
    assert sorted(packaging.values()) == [
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("0.55"),
        Decimal("1.10"),
    ]
    custom = {r.order_line_id: r.amount for r in _by_type(rows, CostType.CUSTOM_DIRECT)}
    assert sorted(custom.values()) == [Decimal("0.00"), Decimal("7.25")]  # NULL -> no row
    null_line = next(
        line.id
        for line in order.lines
        if line.product_id is None and line.custom_direct_cost_estimate is None
    )
    assert null_line not in custom


def test_packaging_comes_from_the_immutable_snapshot_not_current_configuration(
    client, session: Session
):
    business = _setup(client, session)
    juice = purchased_product(session, business)
    order = build_ready_order(
        session, business, [{"product": juice, "qty": "1", "packaging": "0.55"}]
    )
    juice.default_packaging_cost = Decimal("99.00")  # current configuration drifts
    session.commit()
    assert _complete(client, order).status_code == 200
    packaging = _by_type(_ledger(session, order), CostType.PACKAGING)
    assert [r.amount for r in packaging] == [Decimal("0.55")]


def test_production_cost_rows_are_never_created_or_rewritten_by_completion(
    client, session: Session
):
    business = _setup(client, session)
    juice = purchased_product(session, business)
    order = build_ready_order(session, business, [{"product": juice, "qty": "1"}])
    assert _complete(client, order).status_code == 200
    assert _by_type(_ledger(session, order), CostType.PRODUCTION) == []


# ==================================================================== Rejections


def test_non_ready_statuses_are_rejected_with_zero_writes(client, session: Session):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10")
    confirmed = build_ready_order(
        session, business, [{"product": juice, "qty": "1"}], expect_status=None
    )
    confirmed.status = OrderStatus.CONFIRMED
    confirmed.ready_at = None
    session.commit()

    before = domain_fingerprint(session)
    response = _complete(client, confirmed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_COMPLETABLE"
    assert domain_fingerprint(session) == before

    for status in (OrderStatus.DRAFT, OrderStatus.CANCELED):
        order = build_ready_order(
            session, business, [{"product": juice, "qty": "1"}], expect_status=None
        )
        order.status = status
        session.commit()
        before = domain_fingerprint(session)
        response = _complete(client, order)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ORDER_NOT_COMPLETABLE"
        assert domain_fingerprint(session) == before


def test_second_completion_is_rejected_and_does_not_double_consume(client, session: Session):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10", wac="2")
    order = build_ready_order(session, business, [{"product": juice, "qty": "4"}])
    first = _complete(client, order)
    assert first.status_code == 200
    after_first = domain_fingerprint(session)

    # Both a same-version double-submit and a refreshed-version retry are rejected.
    stale = _complete(client, order, version=first.json()["version"] - 1)
    assert stale.status_code == 409
    again = _complete(client, order, version=first.json()["version"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ORDER_NOT_COMPLETABLE"
    assert domain_fingerprint(session) == after_first
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    assert inventory.physical_quantity == Decimal("6")
    assert len(_by_type(_ledger(session, order), CostType.PURCHASED_GOOD)) == 1


def test_stale_version_is_rejected_with_zero_writes(client, session: Session):
    business = _setup(client, session)
    order = build_ready_order(
        session, business, [{"product": purchased_product(session, business), "qty": "1"}]
    )
    before = domain_fingerprint(session)
    response = _complete(client, order, version=order.version + 5)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"
    assert domain_fingerprint(session) == before


def test_malformed_request_is_rejected(client, session: Session):
    business = _setup(client, session)
    order = build_ready_order(
        session, business, [{"product": purchased_product(session, business), "qty": "1"}]
    )
    response = client.post(
        f"/api/v1/orders/{order.id}/complete", json={}, headers=csrf_headers(client)
    )
    assert response.status_code == 422
    assert client.post(
        f"/api/v1/orders/{order.id}/complete", json={"version": order.version}
    ).status_code in (401, 403)  # no CSRF


def test_purchased_stock_lost_without_demotion_is_rejected_with_zero_writes(
    client, session: Session
):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10")
    order = build_ready_order(session, business, [{"product": juice, "qty": "4"}])
    # Stock vanishes out-of-band (bypassing the workflows that would demote the Order).
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    inventory.physical_quantity = Decimal("2")
    session.commit()

    before = domain_fingerprint(session)
    response = _complete(client, order)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_COVERAGE_LOST"
    assert domain_fingerprint(session) == before
    assert reload(session, Order, order.id).status is OrderStatus.READY  # no demotion committed


def test_surplus_lot_shortfall_without_demotion_is_rejected_with_zero_writes(
    client, session: Session
):
    business = _setup(client, session)
    cookie = produced_product(session, business)
    lot = surplus_lot(session, business, cookie, physical="5", basis="1")
    order = build_ready_order(session, business, [{"product": cookie, "qty": "5"}])
    lot.physical_quantity = Decimal("3")  # promised 5, only 3 left, no reconciliation ran
    session.commit()

    before = domain_fingerprint(session)
    response = _complete(client, order)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_COVERAGE_LOST"
    assert domain_fingerprint(session) == before


def test_missing_purchased_reservation_is_rejected(client, session: Session):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10")
    order = build_ready_order(session, business, [{"product": juice, "qty": "4"}])
    for reservation in session.scalars(select(PurchasedProductReservation)):
        session.delete(reservation)
    session.commit()
    before = domain_fingerprint(session)
    response = _complete(client, order)
    assert response.status_code == 409
    codes = {i["code"] for i in response.json()["error"]["issues"]}
    assert "PURCHASED_RESERVATION_MISMATCH" in codes
    assert domain_fingerprint(session) == before


def test_active_run_covering_the_orders_demand_blocks_completion(client, session: Session):
    business = _setup(client, session)
    cookie = produced_product(session, business)
    surplus_lot(session, business, cookie, physical="6", basis="0.5")
    order = build_ready_order(session, business, [{"product": cookie, "qty": "6"}])
    # An active run now protects a requirement that links this Order's line.
    from app.db.models.production import ProductionRequirement, ProductionRequirementOrder
    from app.db.models.recipe import RecipeRevision

    revision = session.scalars(select(RecipeRevision)).one()
    requirement = session.scalars(select(ProductionRequirement)).one()
    link = session.scalars(select(ProductionRequirementOrder)).one()
    assert link.order_line_id == order.lines[0].id  # READY lines stay linked (ADR-139)
    make_production_run(
        session,
        business,
        cookie,
        revision,
        source_production_requirement_id=requirement.id,
    )
    session.commit()

    before = domain_fingerprint(session)
    response = _complete(client, order)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_PRODUCTION_LOCKED"
    assert domain_fingerprint(session) == before


def test_foreign_tenant_order_is_not_found_and_nothing_changes(client, session: Session):
    business = _setup(client, session)
    other = make_business_graph(session, name="Other Business")
    foreign_product = purchased_product(session, other, name="Foreign Juice")
    foreign = build_ready_order(session, other, [{"product": foreign_product, "qty": "2"}])
    before = domain_fingerprint(session)
    response = _complete(client, foreign)
    assert response.status_code == 404
    assert domain_fingerprint(session) == before
    assert business.id != other.id


# ============================================================== Consequences / immutability


def test_history_is_immutable_under_later_changes_and_recalculation(client, session: Session):
    from app.services.operational_recalculation_service import recalculate_product_closure

    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10", wac="1.50")
    order = build_ready_order(
        session, business, [{"product": juice, "qty": "4", "packaging": "0.75"}]
    )
    assert _complete(client, order).status_code == 200
    snapshot = [(r.cost_type, r.amount, r.order_line_id) for r in _ledger(session, order)]

    # Later cost / price / packaging changes, a restock at a new cost, and a recalculation.
    juice.default_packaging_cost = Decimal("50")
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    inventory.weighted_average_unit_cost = Decimal("99")
    session.commit()
    recalculate_product_closure(session, business, juice, business_today=business_today(business))
    session.commit()

    assert [(r.cost_type, r.amount, r.order_line_id) for r in _ledger(session, order)] == snapshot
    body = client.get(f"/api/v1/orders/{order.id}").json()
    assert Decimal(body["historical_cost"]["total"]) == Decimal("6.75")  # 6.00 + 0.75


def test_payments_still_allowed_after_completion_and_cancel_is_rejected(client, session: Session):
    business = _setup(client, session)
    order = build_ready_order(
        session, business, [{"product": purchased_product(session, business), "qty": "1"}]
    )
    assert _complete(client, order).status_code == 200
    payment = client.post(
        f"/api/v1/orders/{order.id}/payments",
        json={
            "amount": "10.00",
            "payment_method": "cash",
            "payment_date": str(business_today(business)),
        },
        headers=csrf_headers(client),
    )
    assert payment.status_code == 201, payment.text
    session.expire_all()
    cancel = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        json={"version": reload(session, Order, order.id).version},
        headers=csrf_headers(client),
    )
    assert cancel.status_code == 409
    assert make_payment is not None


def test_completed_order_leaves_the_dashboard_overdue_and_fulfillment_views(
    client, session: Session
):
    business = _setup(client, session)
    order = build_ready_order(
        session,
        business,
        [{"product": purchased_product(session, business), "qty": "1"}],
        fulfillment_offset_days=-2,
    )
    before = client.get("/api/v1/dashboard").json()
    assert any(a["code"] == "FULFILLMENT_OVERDUE" for a in before["attention"])
    assert _complete(client, order).status_code == 200
    after = client.get("/api/v1/dashboard").json()
    assert not any(a["code"] == "FULFILLMENT_OVERDUE" for a in after["attention"])
    assert after["todays_snapshot"]["orders_overdue"] == 0


def test_get_order_exposes_historical_cost_only_when_completed(client, session: Session):
    business = _setup(client, session)
    order = build_ready_order(
        session, business, [{"product": purchased_product(session, business), "qty": "2"}]
    )
    ready = client.get(f"/api/v1/orders/{order.id}").json()
    assert ready["historical_cost"] is None and ready["completed_at"] is None
    assert _complete(client, order).status_code == 200
    done = client.get(f"/api/v1/orders/{order.id}").json()
    assert done["status"] == "COMPLETED" and done["completed_at"]
    assert set(done["historical_cost"]["by_type"]) == {c.value for c in CostType}
    assert done["estimated_direct_cost"] is None  # Order.estimated_* is never populated


# ===================================================== Atomic rollback at every boundary


@pytest.mark.parametrize(
    "failing_step",
    [
        "_write_ledger_rows",  # after Purchased/Surplus physical deduction + history
        "_finalize_order",  # after ledger creation, before the status change
        "_recalculate_and_reevaluate",  # after ledger + status, before recalculation completes
        "_assert_post_conditions",  # after everything, before commit
    ],
)
def test_failure_at_any_step_rolls_everything_back(
    client, session: Session, monkeypatch, failing_step
):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10", wac="2")
    cookie = produced_product(session, business)
    surplus_lot(session, business, cookie, physical="4", basis="0.5")
    order = build_ready_order(
        session,
        business,
        [
            {"product": juice, "qty": "3", "packaging": "0.40"},
            {"product": cookie, "qty": "4"},
            {"product": None, "qty": "1", "custom_estimate": "2.00"},
        ],
    )
    before = domain_fingerprint(session)

    def boom(*args, **kwargs):
        raise RuntimeError(f"injected failure in {failing_step}")

    monkeypatch.setattr(ocs, failing_step, boom)
    with pytest.raises(RuntimeError, match="injected failure"):
        ocs.complete_order(
            session,
            business,
            order.id,
            expected_version=order.version,
            business_today=business_today(business),
        )
    session.rollback()
    assert domain_fingerprint(session) == before
    assert reload(session, Order, order.id).status is OrderStatus.READY


def test_failure_after_physical_deduction_leaves_inventory_and_history_untouched(
    client, session: Session, monkeypatch
):
    business = _setup(client, session)
    juice = purchased_product(session, business, physical="10")
    order = build_ready_order(session, business, [{"product": juice, "qty": "4"}])
    monkeypatch.setattr(
        ocs, "_write_ledger_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        ocs.complete_order(
            session,
            business,
            order.id,
            expected_version=order.version,
            business_today=business_today(business),
        )
    session.rollback()
    inventory = session.scalars(select(PurchasedProductInventory)).one()
    assert inventory.physical_quantity == Decimal("10")
    assert session.scalars(select(PurchasedProductInventoryTransaction)).all() == []
    assert _ledger(session, order) == []


def test_completed_at_is_a_utc_instant_with_business_local_interpretation(client, session: Session):
    business = _setup(client, session)
    order = build_ready_order(
        session, business, [{"product": purchased_product(session, business), "qty": "1"}]
    )
    body = _complete(client, order).json()
    completed = datetime.fromisoformat(body["completed_at"])
    assert completed.utcoffset() == timedelta(0)
    local = completed.astimezone(__import__("zoneinfo").ZoneInfo(business.timezone))
    assert local.tzinfo is not None


# ================================================== Full chain: production + surplus + purchased


def test_completion_after_real_production_leaves_production_rows_untouched_and_totals_chain(
    client, session: Session
):
    """Order = 12 cookies (4 from existing Surplus, 8 from a real Production Run) + 2 purchased
    + a Custom Item. PRODUCTION rows written by Finish are never touched by Completion; the
    Order's historical direct cost is the sum of all five categories."""
    from tests.integration.api.phase9_support import make_stocked_ingredient

    business = _setup(client, session)
    flour = make_stocked_ingredient(session, business, name="Flour", physical="100000")
    cookie = produced_product(session, business, ingredients=[(flour, Decimal("500"))])
    juice = purchased_product(session, business, physical="5", wac="2.00")
    surplus_lot(session, business, cookie, physical="4", basis="0.50")
    session.commit()

    order = build_ready_order(
        session,
        business,
        [
            {"product": cookie, "qty": "12", "packaging": "0.60"},
            {"product": juice, "qty": "2", "packaging": "0.40"},
            {"product": None, "qty": "1", "custom_estimate": "3.00", "manual_required": False},
        ],
        expect_status=OrderStatus.CONFIRMED,  # the produced remainder still needs production
    )

    from app.db.models.production import ProductionRequirement

    requirement = session.scalars(select(ProductionRequirement)).one()
    start = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert start.status_code == 201, start.text
    run = start.json()
    finish = client.post(
        f"/api/v1/production-runs/{run['id']}/finish",
        json={"version": run["version"], "actual_output_quantity": "12"},
        headers=csrf_headers(client),
    )
    assert finish.status_code == 200, finish.text

    session.expire_all()
    order = reload(session, Order, order.id)
    assert order.status is OrderStatus.READY
    production_before = [
        (r.id, r.amount, r.production_run_order_allocation_id)
        for r in _by_type(_ledger(session, order), CostType.PRODUCTION)
    ]
    assert len(production_before) == 1

    response = _complete(client, order)
    assert response.status_code == 200, response.text
    rows = _ledger(session, order)

    assert [
        (r.id, r.amount, r.production_run_order_allocation_id)
        for r in _by_type(rows, CostType.PRODUCTION)
    ] == production_before  # untouched, never re-priced
    surplus_rows = _by_type(rows, CostType.SURPLUS)
    assert [r.amount for r in surplus_rows] == [Decimal("2.00")]  # 4 x 0.50
    assert [r.amount for r in _by_type(rows, CostType.PURCHASED_GOOD)] == [Decimal("4.00")]
    assert sorted(r.amount for r in _by_type(rows, CostType.PACKAGING)) == [
        Decimal("0.00"),
        Decimal("0.40"),
        Decimal("0.60"),
    ]
    assert [r.amount for r in _by_type(rows, CostType.CUSTOM_DIRECT)] == [Decimal("3.00")]

    body = response.json()["historical_cost"]
    assert Decimal(body["total"]) == sum(r.amount for r in rows)
    expected_total = (
        production_before[0][1]
        + Decimal("2.00")
        + Decimal("4.00")
        + Decimal("1.00")
        + Decimal("3.00")
    )
    assert Decimal(body["total"]) == expected_total
    # No double costing: exactly one ledger row per (category, source).
    keys = [
        (
            r.cost_type,
            r.order_line_id,
            r.surplus_inventory_id,
            r.purchased_inventory_transaction_id,
            r.production_run_order_allocation_id,
        )
        for r in rows
    ]
    assert len(keys) == len(set(keys))
