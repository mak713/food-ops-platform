"""Phase 11 downstream effects and historical immutability (Spec SET-005, ANA-011, AC-ANA-009;
release-blocking).

*Live consumers* of Settings really follow the saved value (Dashboard warning window, Shopping
List default horizon, future Production Starts' labor rate). *Historical truth* never moves:
changing the labor rate or the timezone must not alter a single persisted row of Production
Runs, run ingredients/allocations, `OrderCostAllocation`, completed Orders/Lines — proven with
whole-row fingerprints — nor any Analytics Revenue / Direct Cost / Contribution number (labor).
A timezone change legitimately re-interprets the UTC instants in the new Business-local zone
(period bounds, "today") without rewriting a persisted fact.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.v1 import dashboard as dashboard_router
from app.db.enums import OrderStatus
from app.db.models.production import ProductionRequirement, ProductionRun
from tests.integration.api import analytics_support
from tests.integration.api.analytics_support import setup_golden
from tests.integration.api.completion_support import (
    build_ready_order,
    domain_fingerprint,
    produced_product,
)
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.api.phase9_support import (
    business_by_name,
    business_today,
    confirm_demand,
    make_recipe_product,
    make_stocked_ingredient,
)

D = Decimal
SETTINGS = "/api/v1/settings/business"
DASHBOARD = "/api/v1/dashboard"
SHOPPING = "/api/v1/shopping-list"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)  # 08:00 America/New_York
TODAY = date(2026, 6, 10)
ANALYTICS = "/api/v1/analytics"
P7 = {"range": "LAST_7_DAYS"}


def _patch(client, **fields):
    version = client.get("/api/v1/settings").json()["version"]
    response = client.patch(
        SETTINGS, json={"version": version, **fields}, headers=csrf_headers(client)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _alert(body: dict, code: str) -> dict | None:
    return next((a for a in body["attention"] if a["code"] == code), None)


def _fp(session: Session, table: str, where: str = "true") -> tuple[int, str]:
    session.expire_all()
    row = session.execute(
        text(
            f"SELECT count(*), coalesce(md5(string_agg(t::text, ',' ORDER BY t::text)), '') "
            f"FROM {table} t WHERE {where}"
        )
    ).one()
    return row[0], row[1]


# ------------------------------------------------------------------------------ Dashboard


@pytest.fixture()
def freeze_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard_router, "_utc_now", lambda: NOW)


def test_dashboard_fulfillment_risk_follows_the_saved_warning_minutes(
    client, session, freeze_dashboard
):
    signup(client)
    business = business_by_name(session)
    cookie = make_recipe_product(session, business, name="Cookie")
    for number, hour, minute in (("ORD-RISK", 8, 30), ("ORD-NOON", 12, 0)):
        confirm_demand(
            session,
            business,
            cookie,
            quantity="12",
            fulfillment_date=TODAY,
            fulfillment_time=time(hour, minute),
            today=TODAY,
            order_number=number,
        )

    def risky() -> set[str]:
        body = client.get(DASHBOARD).json()
        alert = _alert(body, "FULFILLMENT_AT_RISK")
        return set() if alert is None else {i["label"] for i in alert["items"]}

    assert risky() == {"ORD-RISK"}  # default 60-minute window
    _patch(client, fulfillment_warning_minutes=15)
    assert risky() == set()  # 30 minutes away is outside a 15-minute window
    _patch(client, fulfillment_warning_minutes=30)
    assert risky() == {"ORD-RISK"}  # inclusive boundary
    _patch(client, fulfillment_warning_minutes=300)
    assert risky() == {"ORD-RISK", "ORD-NOON"}


def test_extreme_valid_warning_minutes_never_break_the_dashboard(client, session, freeze_dashboard):
    signup(client)
    business = business_by_name(session)
    cookie = make_recipe_product(session, business, name="Cookie")
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=TODAY,
        fulfillment_time=time(12, 0),
        today=TODAY,
        order_number="ORD-X",
    )
    saved = _patch(client, fulfillment_warning_minutes=2_147_483_647)
    assert saved["fulfillment_warning_minutes"] == 2_147_483_647
    response = client.get(DASHBOARD)
    assert response.status_code == 200, response.text
    alert = _alert(response.json(), "FULFILLMENT_AT_RISK")
    assert alert is not None and alert["items"][0]["label"] == "ORD-X"


# ---------------------------------------------------------------------------- Shopping List


def test_shopping_list_default_range_follows_the_saved_horizon(client, session):
    signup(client)
    business = business_by_name(session)
    today = business_today(business)

    seven = client.get(SHOPPING).json()["range"]
    assert seven["horizon_days"] == 7
    assert seven["end_date"] == (today + timedelta(days=7)).isoformat()

    _patch(client, shopping_horizon_days=3)
    three = client.get(SHOPPING).json()["range"]
    assert three["horizon_days"] == 3 and three["is_custom"] is False
    assert three["end_date"] == (today + timedelta(days=3)).isoformat()

    # an explicit request is unaffected by the saved default
    explicit = client.get(f"{SHOPPING}?horizon_days=7").json()["range"]
    assert explicit["horizon_days"] == 7


# -------------------------------------------------------------------- labor-rate immutability

_HISTORY_TABLES = (
    "production_runs",
    "production_run_ingredients",
    "production_run_order_allocations",
    "order_cost_allocations",
    "orders",
    "order_lines",
    "production_requirements",
    "production_requirement_orders",
    "inventory_transactions",
)


def _analytics_snapshot(client) -> dict[str, bytes]:
    out = {}
    for path in ("overview", "trends", "products", "customers", "orders"):
        params = {**P7, "compare": "true"} if path == "overview" else P7
        response = client.get(f"{ANALYTICS}/{path}", params=params)
        assert response.status_code == 200, (path, response.text)
        out[path] = response.content
    for path in ("products.csv", "orders.csv"):
        response = client.get(f"{ANALYTICS}/{path}", params=P7)
        assert response.status_code == 200
        out[path] = response.content
    return out


def _start(client, requirement: ProductionRequirement) -> dict:
    response = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_labor_rate_change_never_rewrites_history_and_applies_to_future_starts(
    client, session, monkeypatch
):
    signup(client)
    business = business_by_name(session)
    flour = make_stocked_ingredient(session, business, name="Flour", physical="100000")
    cookie = produced_product(session, business, name="Cookie", ingredients=[(flour, D("500"))])
    brownie = produced_product(session, business, name="Brownie", ingredients=[(flour, D("400"))])
    session.commit()

    _patch(client, default_labor_rate="20.00")  # rate A

    order1 = build_ready_order(
        session,
        business,
        [{"product": cookie, "qty": "12", "packaging": "0.60"}],
        expect_status=OrderStatus.CONFIRMED,
    )
    req1 = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == cookie.id)
    ).one()
    run1 = _start(client, req1)
    finish = client.post(
        f"/api/v1/production-runs/{run1['id']}/finish",
        json={"version": run1["version"], "actual_output_quantity": "12"},
        headers=csrf_headers(client),
    )
    assert finish.status_code == 200, finish.text
    session.expire_all()
    session.refresh(order1)
    complete = client.post(
        f"/api/v1/orders/{order1.id}/complete",
        json={"version": order1.version},
        headers=csrf_headers(client),
    )
    assert complete.status_code == 200, complete.text

    # A second, not-yet-started requirement whose planned estimate was calculated at rate A.
    build_ready_order(
        session,
        business,
        [{"product": brownie, "qty": "12", "packaging": "0.00"}],
        expect_status=OrderStatus.CONFIRMED,
    )
    req2 = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.product_id == brownie.id)
    ).one()
    planned_labor_at_a = req2.estimated_labor_cost

    stored_run1 = session.scalars(select(ProductionRun).where(ProductionRun.id == run1["id"])).one()
    assert stored_run1.labor_rate_snapshot == D("20.00")
    actual_labor_a = stored_run1.actual_labor_cost
    assert actual_labor_a is not None and actual_labor_a > 0

    # Analytics `generated_at` comes from the router clock: pin it just after Completion so
    # the before/after responses can be compared byte-for-byte.
    analytics_support.freeze(monkeypatch, datetime.now(UTC) + timedelta(minutes=1))

    before_tables = {t: _fp(session, t) for t in _HISTORY_TABLES}
    before_analytics = _analytics_snapshot(client)

    saved = _patch(client, default_labor_rate="35.00")  # rate B
    assert saved["default_labor_rate"] == "35.00"

    after_tables = {t: _fp(session, t) for t in _HISTORY_TABLES}
    assert after_tables == before_tables, "a labor-rate change rewrote persisted history"
    assert _analytics_snapshot(client) == before_analytics

    session.expire_all()
    stored_run1 = session.scalars(select(ProductionRun).where(ProductionRun.id == run1["id"])).one()
    assert stored_run1.labor_rate_snapshot == D("20.00")
    assert stored_run1.actual_labor_cost == actual_labor_a
    stored_req2 = session.scalars(
        select(ProductionRequirement).where(ProductionRequirement.id == req2.id)
    ).one()
    assert stored_req2.estimated_labor_cost == planned_labor_at_a  # ADR-131 snapshot, not rewritten

    # A new Production Start uses the new current rate.
    run2 = _start(client, stored_req2)
    session.expire_all()
    stored_run2 = session.scalars(select(ProductionRun).where(ProductionRun.id == run2["id"])).one()
    assert stored_run2.labor_rate_snapshot == D("35.00")
    assert stored_run2.estimated_labor_cost > planned_labor_at_a
    # ... and starting it did not touch the earlier run.
    stored_run1_after = session.scalars(
        select(ProductionRun).where(ProductionRun.id == run1["id"])
    ).one()
    assert stored_run1_after.labor_rate_snapshot == D("20.00")
    assert stored_run1_after.actual_labor_cost == actual_labor_a


def test_extreme_labor_rate_yields_a_structured_outcome_never_a_500(client, session):
    """Exploratory (plan §D.5): a maximum-magnitude NUMERIC(14,2) rate must not crash the
    Production Start with an unhandled error."""
    signup(client)
    business = business_by_name(session)
    flour = make_stocked_ingredient(session, business, name="Flour", physical="100000")
    cookie = produced_product(session, business, name="Cookie", ingredients=[(flour, D("500"))])
    session.commit()
    build_ready_order(
        session,
        business,
        [{"product": cookie, "qty": "12"}],
        expect_status=OrderStatus.CONFIRMED,
    )
    requirement = session.scalars(select(ProductionRequirement)).one()
    _patch(client, default_labor_rate="999999999999.99")
    response = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert response.status_code < 500, response.text
    if response.status_code >= 400:
        assert "code" in response.json()["error"]


# ------------------------------------------------------------------- timezone immutability


def test_timezone_change_reinterprets_but_never_rewrites_persisted_facts(
    client, session, monkeypatch
):
    golden = setup_golden(client, session, monkeypatch)  # Chicago; clock frozen 18:00Z Oct 16
    monkeypatch.setattr(dashboard_router, "_utc_now", lambda: analytics_support.NOW)
    del golden

    def tables() -> dict[str, tuple[int, str]]:
        session.expire_all()
        return {k: v for k, v in domain_fingerprint(session).items() if k != "businesses"}

    completed_before = session.execute(
        text("select id::text, completed_at::text from orders order by id")
    ).all()
    persisted_before = tables()
    overview_before = client.get(f"{ANALYTICS}/overview", params=P7).json()
    dashboard_before = client.get(DASHBOARD).json()
    assert overview_before["timezone"] == "America/Chicago"
    assert overview_before["period"]["end_date"] == "2026-10-16"
    assert dashboard_before["business_today"] == "2026-10-16"

    saved = _patch(client, timezone="Pacific/Auckland")  # 18:00Z Oct 16 == 07:00 Oct 17 NZDT
    assert saved["timezone"] == "Pacific/Auckland"

    # Persisted facts: byte-for-byte unchanged.
    assert tables() == persisted_before
    assert (
        session.execute(text("select id::text, completed_at::text from orders order by id")).all()
        == completed_before
    )

    # Business-local interpretation now uses the new zone, as specified.
    overview_after = client.get(f"{ANALYTICS}/overview", params=P7).json()
    assert overview_after["timezone"] == "Pacific/Auckland"
    assert overview_after["period"]["end_date"] == "2026-10-17"
    assert (
        overview_after["period"]["lower_bound_utc"] != overview_before["period"]["lower_bound_utc"]
    )
    dashboard_after = client.get(DASHBOARD).json()
    assert dashboard_after["timezone"] == "Pacific/Auckland"
    assert dashboard_after["business_today"] == "2026-10-17"
    assert client.get("/api/v1/auth/me").json()["business"]["timezone"] == "Pacific/Auckland"


def test_invalid_timezone_change_is_rejected_and_nothing_changes(client, session):
    signup(client)
    before = _fp(session, "businesses")
    response = client.patch(
        SETTINGS,
        json={"version": 1, "timezone": "Mars/Phobos"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert _fp(session, "businesses") == before
