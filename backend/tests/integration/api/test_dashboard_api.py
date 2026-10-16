"""Phase 9 Dashboard API tests (Acceptance Matrix D1–D9, X4): six sections in spec order,
every alert code (trigger / non-trigger), aggregation, fulfillment risk, missing time,
advance prep, expiring Surplus, restock effects, Business-timezone boundaries, and
consistency with the Shopping List.

Time is frozen at 2026-06-10 12:00 UTC == 08:00 America/New_York (EDT) by replacing the
router's `_utc_now`, so every timing rule is deterministic (Spec §19.13).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.api.v1 import dashboard as dashboard_router
from app.db.enums import OrderStatus, ProductionRunStatus, ProductType
from app.services import shopping_list_service
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.api.phase9_support import (
    business_by_name,
    confirm_demand,
    make_recipe_product,
    make_stocked_ingredient,
)
from tests.integration.schema.factories import (
    make_customer,
    make_ingredient,
    make_order_line,
    make_order_status_history,
    make_payment,
    make_product,
    make_production_run,
    make_surplus_inventory,
)

URL = "/api/v1/dashboard"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)  # 08:00 EDT
TODAY = date(2026, 6, 10)


@pytest.fixture()
def freeze(monkeypatch):
    def _freeze(at: datetime = NOW) -> None:
        monkeypatch.setattr(dashboard_router, "_utc_now", lambda: at)

    _freeze()
    return _freeze


def _setup(client, session: Session):
    signup(client)
    return business_by_name(session)


def _dash(client) -> dict:
    response = client.get(URL)
    assert response.status_code == 200, response.text
    return response.json()


def _alert(body: dict, code: str) -> dict | None:
    return next((a for a in body["attention"] if a["code"] == code), None)


def _cookie(session, business, **kw):
    return make_recipe_product(session, business, name=kw.pop("name", "Cookie"), **kw)


def _confirm(session, business, product, *, qty="12", d=TODAY, t=None, **kw):
    return confirm_demand(
        session,
        business,
        product,
        quantity=qty,
        fulfillment_date=d,
        fulfillment_time=t,
        today=TODAY,
        **kw,
    )


# ------------------------------------------------------------------------ structure (D1)


def test_empty_dashboard_has_no_content_in_any_section(client, session, freeze):
    _setup(client, session)
    body = _dash(client)
    assert body["attention"] == []
    assert body["active_production"] == []
    assert body["todays_production"] == {"items": [], "manual_workload": None}
    assert body["todays_fulfillments"] == []
    assert body["upcoming"] == []
    snap = body["todays_snapshot"]
    assert snap["orders_due_today"] == 0 and snap["active_runs"] == 0
    assert body["business_today"] == "2026-06-10" and body["timezone"] == "America/New_York"


def test_sections_are_returned_in_spec_priority_order(client, session, freeze):
    _setup(client, session)
    keys = list(_dash(client))
    sections = [
        k
        for k in keys
        if k
        in {
            "attention",
            "active_production",
            "todays_production",
            "todays_fulfillments",
            "todays_snapshot",
            "upcoming",
        }
    ]
    assert sections == [
        "attention",
        "active_production",
        "todays_production",
        "todays_fulfillments",
        "todays_snapshot",
        "upcoming",
    ]


def test_snapshot_is_operational_only_no_analytics_metrics(client, session, freeze):
    """Approved Q2: no revenue / direct cost / contribution / AOV / completed metric."""
    _setup(client, session)
    assert set(_dash(client)["todays_snapshot"]) == {
        "orders_due_today",
        "orders_ready",
        "orders_awaiting",
        "orders_at_risk",
        "orders_overdue",
        "scheduled_order_value_today",
        "production_items_today",
        "planned_batches_today",
        "active_runs",
    }


def test_unauthenticated_is_rejected(client, session):
    assert client.get(URL).status_code == 401


# ---------------------------------------------------------------- alert aggregation (D2)


def test_many_shortages_aggregate_into_one_alert_with_true_count(client, session, freeze):
    business = _setup(client, session)
    ingredients = [
        (make_stocked_ingredient(session, business, name=f"Ing{i}", physical="1"), Decimal("100"))
        for i in range(7)
    ]
    cookie = _cookie(session, business, ingredients=ingredients)
    _confirm(session, business, cookie)

    body = _dash(client)
    shortage_alerts = [a for a in body["attention"] if a["code"] == "INGREDIENT_SHORTAGE"]
    assert len(shortage_alerts) == 1
    alert = shortage_alerts[0]
    assert alert["count"] == 7
    assert len(alert["items"]) == 5 and alert["items_truncated"] is True
    assert alert["destination_path"] == "/app/production/shopping-list"
    assert alert["severity"] == "WARNING"
    assert alert["condition"] and alert["impact"]


def test_alert_ordering_is_warnings_first_then_notices(client, session, freeze):
    business = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="1")
    cookie = _cookie(session, business, ingredients=[(flour, Decimal("50"))])
    _confirm(session, business, cookie, d=TODAY - timedelta(days=1))
    severities = [a["severity"] for a in _dash(client)["attention"]]
    assert severities == sorted(severities, key=lambda s: 0 if s == "WARNING" else 1)
    assert "NOTICE" in severities and "WARNING" in severities


# ------------------------------------------------------------------- per-code alerts (D3)


def test_overdue_alert_for_confirmed_and_ready_but_not_today_or_future(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    _confirm(session, business, cookie, d=TODAY - timedelta(days=1), order_number="ORD-LATE1")
    _confirm(
        session,
        business,
        cookie,
        d=TODAY - timedelta(days=3),
        order_number="ORD-LATE2",
        status=OrderStatus.READY,
    )
    _confirm(session, business, cookie, d=TODAY, order_number="ORD-TODAY")
    _confirm(session, business, cookie, d=TODAY + timedelta(days=1), order_number="ORD-LATER")

    alert = _alert(_dash(client), "FULFILLMENT_OVERDUE")
    assert alert["count"] == 2
    assert {i["label"] for i in alert["items"]} == {"ORD-LATE1", "ORD-LATE2"}
    assert all(i["path"].startswith("/app/orders/") for i in alert["items"])


def test_at_risk_uses_business_warning_minutes_and_excludes_ready_and_date_only(
    client, session, freeze
):
    business = _setup(client, session)  # warning minutes default 60; now = 08:00 local
    cookie = _cookie(session, business)
    _confirm(session, business, cookie, t=time(8, 30), order_number="ORD-RISK")
    _confirm(session, business, cookie, t=time(12, 0), order_number="ORD-FINE")
    _confirm(
        session, business, cookie, t=time(8, 30), order_number="ORD-READY", status=OrderStatus.READY
    )
    _confirm(session, business, cookie, t=None, order_number="ORD-NOTIME")

    alert = _alert(_dash(client), "FULFILLMENT_AT_RISK")
    assert alert["count"] == 1 and alert["items"][0]["label"] == "ORD-RISK"


def test_at_risk_window_follows_the_configured_warning_minutes(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    _confirm(session, business, cookie, t=time(12, 0), order_number="ORD-NOON")
    assert _alert(_dash(client), "FULFILLMENT_AT_RISK") is None  # 60-minute window

    business.fulfillment_warning_minutes = 300  # now inside a 5-hour window
    session.commit()
    alert = _alert(_dash(client), "FULFILLMENT_AT_RISK")
    assert alert is not None and alert["items"][0]["label"] == "ORD-NOON"


def test_lost_ready_alert_for_confirmed_order_with_ready_history(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    demoted = _confirm(session, business, cookie, d=TODAY + timedelta(days=2), order_number="ORD-D")
    make_order_status_history(session, business, demoted, to_status=OrderStatus.READY)
    _confirm(session, business, cookie, d=TODAY + timedelta(days=2), order_number="ORD-NEVER")
    session.commit()

    alert = _alert(_dash(client), "ORDER_LOST_READY")
    assert alert["count"] == 1 and alert["items"][0]["label"] == "ORD-D"
    # Neutral wording: only what the data proves (CONFIRMED now, READY in its history).
    text = " ".join(
        [alert["title"], alert["condition"], alert["impact"], alert["items"][0]["detail"]]
    )
    assert "no longer Ready" in text
    for unsupported in ("supply", "reduced", "inventory", "surplus"):
        assert unsupported not in text.lower()


def test_ingredient_shortage_alert_clears_after_restock(client, session, freeze):
    business = _setup(client, session)
    flour = make_stocked_ingredient(session, business, physical="10")
    cookie = _cookie(session, business, ingredients=[(flour, Decimal("100"))])
    _confirm(session, business, cookie)

    alert = _alert(_dash(client), "INGREDIENT_SHORTAGE")
    assert alert["count"] == 1 and "short 90 g" in alert["items"][0]["detail"]

    response = client.post(
        f"/api/v1/ingredients/{flour.id}/inventory/restock",
        json={"version": flour.version, "quantity": "500", "unit": "g", "unit_cost": "0.02"},
        headers=csrf_headers(client),
    )
    assert response.status_code in (200, 201), response.text
    assert _alert(_dash(client), "INGREDIENT_SHORTAGE") is None


def test_purchased_shortage_alert_clears_after_purchased_restock(client, session, freeze):
    business = _setup(client, session)
    jam = make_product(session, business, product_type=ProductType.PURCHASED, name="Jam")
    session.flush()
    _confirm(session, business, jam, qty="5")

    alert = _alert(_dash(client), "PURCHASED_PRODUCT_SHORTAGE")
    assert alert["count"] == 1 and alert["items"][0]["label"] == "Jam"
    assert "short 5" in alert["items"][0]["detail"]

    response = client.post(
        f"/api/v1/products/{jam.id}/purchased-inventory/restock",
        json={"version": None, "quantity": "10", "unit_cost": "2.00"},
        headers=csrf_headers(client),
    )
    assert response.status_code in (200, 201), response.text
    assert _alert(_dash(client), "PURCHASED_PRODUCT_SHORTAGE") is None


def test_missing_recipe_alert_names_product_and_does_not_invent_a_shortage(client, session, freeze):
    business = _setup(client, session)
    loaf = make_product(session, business, name="Mystery Loaf")
    session.flush()
    _confirm(session, business, loaf, qty="6")

    body = _dash(client)
    alert = _alert(body, "PRODUCT_MISSING_RECIPE")
    assert alert["count"] == 1 and alert["items"][0]["label"] == "Mystery Loaf"
    assert _alert(body, "INGREDIENT_SHORTAGE") is None


def test_negative_ingredient_balance_alert(client, session, freeze):
    business = _setup(client, session)
    owed = make_ingredient(session, business, name="Butter")
    owed.physical_quantity = Decimal("-25")
    fine = make_ingredient(session, business, name="Flour")
    fine.physical_quantity = Decimal("5")
    session.commit()

    alert = _alert(_dash(client), "NEGATIVE_INGREDIENT_INVENTORY")
    assert alert["count"] == 1
    assert alert["items"][0]["label"] == "Butter" and "-25" in alert["items"][0]["detail"]


def test_missing_time_alert_only_within_next_seven_days_and_only_when_time_absent(
    client, session, freeze
):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    _confirm(session, business, cookie, d=TODAY, order_number="ORD-A")
    _confirm(session, business, cookie, d=TODAY + timedelta(days=7), order_number="ORD-B")
    _confirm(session, business, cookie, d=TODAY + timedelta(days=8), order_number="ORD-C")
    _confirm(
        session, business, cookie, d=TODAY + timedelta(days=2), t=time(9, 0), order_number="ORD-D"
    )

    alert = _alert(_dash(client), "MISSING_FULFILLMENT_TIME")
    assert {i["label"] for i in alert["items"]} == {"ORD-A", "ORD-B"}


def test_missing_time_never_fabricates_a_suggested_start(client, session, freeze):
    business = _setup(client, session)
    rolls = _cookie(session, business, name="Rolls", elapsed_time_minutes=240)
    _confirm(session, business, rolls, t=None)
    item = _dash(client)["todays_production"]["items"][0]
    assert item["timing"] == "DUE_TODAY"
    assert item["suggested_start_at"] is None


def test_expiring_surplus_alert_window_and_eligibility(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business, name="Cookie")
    brownie = _cookie(session, business, name="Brownie")
    muffin = _cookie(session, business, name="Muffin")
    scone = _cookie(session, business, name="Scone")
    make_surplus_inventory(session, business, cookie, physical_quantity=6).usable_through_date = (
        TODAY + timedelta(days=1)
    )
    make_surplus_inventory(session, business, brownie, physical_quantity=6).usable_through_date = (
        TODAY + timedelta(days=3)
    )
    nonreusable = make_surplus_inventory(session, business, muffin, physical_quantity=6)
    nonreusable.usable_through_date = TODAY
    nonreusable.is_reusable = False
    make_surplus_inventory(session, business, scone, physical_quantity=6).usable_through_date = (
        TODAY - timedelta(days=1)
    )
    session.commit()

    alert = _alert(_dash(client), "SURPLUS_EXPIRING")
    assert alert["count"] == 1 and alert["items"][0]["label"] == "Cookie"
    assert alert["severity"] == "NOTICE"
    assert "6 available" in alert["items"][0]["detail"]


def test_fully_allocated_expiring_lot_is_not_flagged(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    lot = make_surplus_inventory(session, business, cookie, physical_quantity=6)
    lot.usable_through_date = TODAY + timedelta(days=1)
    session.flush()
    confirm_demand(
        session,
        business,
        cookie,
        quantity="6",
        fulfillment_date=TODAY + timedelta(days=1),
        today=TODAY,
    )
    assert _alert(_dash(client), "SURPLUS_EXPIRING") is None


# ----------------------------------------------------------- production sections (D5/D6)


def test_start_overdue_due_today_and_advance_prep_classification(client, session, freeze):
    business = _setup(client, session)  # now = 08:00 local
    overdue = _cookie(session, business, name="Overdue Loaf", elapsed_time_minutes=120)
    due = _cookie(session, business, name="Due Cookie", elapsed_time_minutes=120)
    rolls = _cookie(session, business, name="Cinnamon Rolls", elapsed_time_minutes=1440)
    later = _cookie(session, business, name="Later Cake", elapsed_time_minutes=60)

    _confirm(session, business, overdue, t=time(9, 0))  # start 07:00 -> already passed
    _confirm(session, business, due, t=time(17, 0))  # start 15:00 today
    _confirm(
        session, business, rolls, d=TODAY + timedelta(days=1), t=time(10, 0)
    )  # start today 10:00
    _confirm(
        session, business, later, d=TODAY + timedelta(days=2), t=time(10, 0)
    )  # start d+2 09:00

    body = _dash(client)
    by_name = {i["product_name"]: i for i in body["todays_production"]["items"]}
    assert set(by_name) == {"Overdue Loaf", "Due Cookie", "Cinnamon Rolls"}
    assert by_name["Overdue Loaf"]["timing"] == "START_OVERDUE"
    assert by_name["Due Cookie"]["timing"] == "DUE_TODAY"
    assert by_name["Cinnamon Rolls"]["timing"] == "ADVANCE_PREP"
    assert by_name["Cinnamon Rolls"]["demand_date"] == (TODAY + timedelta(days=1)).isoformat()
    # Overdue first, then due today, then advance prep.
    assert [i["timing"] for i in body["todays_production"]["items"]] == [
        "START_OVERDUE",
        "DUE_TODAY",
        "ADVANCE_PREP",
    ]
    alert = _alert(body, "PRODUCTION_START_OVERDUE")
    assert alert["count"] == 1 and alert["items"][0]["label"] == "Overdue Loaf"
    assert alert["severity"] == "NOTICE"


def test_started_requirement_is_not_offered_in_todays_production(client, session, freeze):
    from sqlalchemy import select

    from app.db.models.production import ProductionRequirement

    business = _setup(client, session)
    cookie = _cookie(session, business, elapsed_time_minutes=60)
    _confirm(session, business, cookie, t=time(17, 0))
    requirement = session.scalars(select(ProductionRequirement)).one()
    response = client.post(
        "/api/v1/production-runs",
        json={"production_requirement_id": str(requirement.id)},
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text

    body = _dash(client)
    assert body["todays_production"]["items"] == []
    assert len(body["active_production"]) == 1


def test_active_production_lists_only_in_production_runs_with_guidance(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    from sqlalchemy import select

    from app.db.models.recipe import RecipeRevision

    revision = session.scalars(select(RecipeRevision)).one()
    running = make_production_run(
        session,
        business,
        cookie,
        revision,
        planned_batches=2,
        started_at=NOW - timedelta(minutes=150),
    )
    running.elapsed_minutes_per_batch_snapshot = 60
    make_production_run(
        session,
        business,
        cookie,
        revision,
        status=ProductionRunStatus.COMPLETED,
        started_at=NOW - timedelta(hours=5),
    )
    make_production_run(
        session,
        business,
        cookie,
        revision,
        status=ProductionRunStatus.CANCELED,
        started_at=NOW - timedelta(hours=5),
    )
    session.commit()

    runs = _dash(client)["active_production"]
    assert len(runs) == 1
    run = runs[0]
    assert run["product_name"] == "Cookie" and run["planned_batches"] == 2
    assert run["elapsed_minutes"] == 150
    assert run["expected_elapsed_minutes"] == 120
    assert run["is_past_expected"] is True


def test_manual_custom_item_workload_is_labeled_and_excludes_satisfied_lines(
    client, session, freeze
):
    from app.db.enums import OrderLineType
    from tests.integration.schema.factories import make_order

    business = _setup(client, session)
    order = make_order(session, business, status=OrderStatus.CONFIRMED)
    order.fulfillment_date = TODAY
    a = make_order_line(session, business, order, line_type=OrderLineType.CUSTOM_ITEM)
    a.custom_active_time_minutes = 45
    a.manual_fulfillment_required = True
    b = make_order_line(session, business, order, line_type=OrderLineType.CUSTOM_ITEM)
    b.custom_active_time_minutes = 30
    b.manual_fulfillment_required = True
    b.manual_fulfillment_satisfied = True  # already done -> no longer workload
    session.commit()

    workload = _dash(client)["todays_production"]["manual_workload"]
    assert workload == {"total_minutes": 45, "line_count": 1, "order_count": 1}


# ------------------------------------------------------------ fulfillments / snapshot (D7/D8)


def test_todays_fulfillments_order_status_payment_and_blockers(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    jam = make_product(session, business, product_type=ProductType.PURCHASED, name="Jam")
    alice = make_customer(session, business, name="Alice")
    session.flush()

    late = _confirm(
        session, business, cookie, t=time(15, 0), order_number="ORD-3", customer_id=alice.id
    )
    early = _confirm(session, business, cookie, t=time(9, 0), order_number="ORD-1")
    untimed = _confirm(session, business, jam, qty="2", t=None, order_number="ORD-4")
    ready = _confirm(
        session, business, cookie, t=time(11, 0), order_number="ORD-2", status=OrderStatus.READY
    )
    _confirm(session, business, cookie, d=TODAY + timedelta(days=1), order_number="ORD-TOMORROW")
    _confirm(session, business, cookie, status=OrderStatus.DRAFT, order_number="ORD-DRAFT")
    _confirm(session, business, cookie, status=OrderStatus.CANCELED, order_number="ORD-CANCELED")
    make_payment(session, business, late, amount="10.00")
    make_payment(session, business, ready, amount="25.00")
    session.commit()
    assert early and untimed

    items = _dash(client)["todays_fulfillments"]
    assert [i["order_number"] for i in items] == ["ORD-1", "ORD-2", "ORD-3", "ORD-4"]
    by_number = {i["order_number"]: i for i in items}

    assert by_number["ORD-3"]["customer_name"] == "Alice"
    assert by_number["ORD-1"]["customer_name"] is None  # Guest
    assert by_number["ORD-3"]["payment_status"] == "PARTIALLY_PAID"
    assert by_number["ORD-2"]["payment_status"] == "PAID"
    assert by_number["ORD-1"]["payment_status"] == "UNPAID"

    assert by_number["ORD-2"]["status"] == "READY" and by_number["ORD-2"]["unsatisfied_lines"] == []
    assert by_number["ORD-1"]["unsatisfied_lines"][0]["reason"] == "PRODUCTION_PENDING"
    assert by_number["ORD-4"]["unsatisfied_lines"][0]["reason"] == "PURCHASED_STOCK_SHORT"
    assert by_number["ORD-4"]["fulfillment_time"] is None


def test_snapshot_counts_and_scheduled_value_match_underlying_orders(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    _confirm(session, business, cookie, t=time(8, 30), final_total="40.00")  # at risk, awaiting
    _confirm(session, business, cookie, t=time(18, 0), final_total="35.50")  # awaiting
    _confirm(
        session, business, cookie, t=time(11, 0), status=OrderStatus.READY, final_total="10.00"
    )
    _confirm(session, business, cookie, d=TODAY - timedelta(days=2), final_total="99.00")  # overdue
    _confirm(session, business, cookie, d=TODAY + timedelta(days=1), final_total="500.00")
    _confirm(session, business, cookie, status=OrderStatus.DRAFT, final_total="777.00")

    snap = _dash(client)["todays_snapshot"]
    assert snap["orders_due_today"] == 3
    assert snap["orders_ready"] == 1
    assert snap["orders_awaiting"] == 2
    assert snap["orders_at_risk"] == 1
    assert snap["orders_overdue"] == 1
    assert Decimal(snap["scheduled_order_value_today"]) == Decimal("85.50")


def test_upcoming_covers_the_next_seven_days_groups_by_date_and_excludes_drafts(
    client, session, freeze
):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    _confirm(
        session,
        business,
        cookie,
        d=TODAY + timedelta(days=1),
        t=time(9, 0),
        order_number="ORD-A",
        final_total="20.00",
    )
    _confirm(
        session,
        business,
        cookie,
        d=TODAY + timedelta(days=1),
        order_number="ORD-B",
        final_total="30.00",
    )
    _confirm(session, business, cookie, d=TODAY + timedelta(days=7), order_number="ORD-C")
    _confirm(session, business, cookie, d=TODAY + timedelta(days=8), order_number="ORD-OUT")
    _confirm(
        session,
        business,
        cookie,
        d=TODAY + timedelta(days=2),
        status=OrderStatus.DRAFT,
        order_number="ORD-DRAFT",
    )
    _confirm(session, business, cookie, d=TODAY, order_number="ORD-TODAY")

    days = _dash(client)["upcoming"]
    assert [d["date"] for d in days] == [
        (TODAY + timedelta(days=1)).isoformat(),
        (TODAY + timedelta(days=7)).isoformat(),
    ]
    first = days[0]
    assert first["order_count"] == 2
    assert Decimal(first["scheduled_order_value"]) == Decimal("50.00")
    assert [o["order_number"] for o in first["orders"]] == [
        "ORD-A",
        "ORD-B",
    ]  # timed before untimed
    assert first["production_item_count"] == 1


def test_upcoming_order_list_is_capped_with_true_count(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    for i in range(12):
        _confirm(
            session, business, cookie, d=TODAY + timedelta(days=3), order_number=f"ORD-{i:02d}"
        )
    day = _dash(client)["upcoming"][0]
    assert day["order_count"] == 12
    assert len(day["orders"]) == 10 and day["orders_truncated"] is True


# --------------------------------------------------------------- consistency (D9)


def test_dashboard_and_shopping_list_report_the_same_shortage(client, session, freeze):
    business = _setup(client, session)
    flour = make_stocked_ingredient(session, business, name="Flour", physical="100")
    butter = make_stocked_ingredient(session, business, name="Butter", physical="20")
    cookie = _cookie(
        session, business, ingredients=[(flour, Decimal("60")), (butter, Decimal("50"))]
    )
    _confirm(session, business, cookie, d=TODAY)
    _confirm(session, business, cookie, d=TODAY + timedelta(days=2))

    report = shopping_list_service.get_shopping_list(session, business, business_today=TODAY)
    expected = {i.ingredient_name: i.shortage_quantity for i in report.shortages}
    assert expected  # a real shortage exists

    alert = _alert(_dash(client), "INGREDIENT_SHORTAGE")
    assert alert["count"] == len(expected)
    for item in alert["items"]:
        qty = expected[item["label"]]
        assert f"short {format(qty.normalize(), 'f')} g" in item["detail"]


# ----------------------------------------------------------- timezone boundaries (X4)


def test_business_local_date_not_utc_date_defines_today(client, session, freeze):
    business = _setup(client, session)
    cookie = _cookie(session, business)
    _confirm(session, business, cookie, d=date(2026, 6, 10), order_number="ORD-X")

    # 03:30 UTC on 06-11 == 23:30 EDT on 06-10 -> the Business is still on 06-10.
    freeze(datetime(2026, 6, 11, 3, 30, tzinfo=UTC))
    body = _dash(client)
    assert body["business_today"] == "2026-06-10"
    assert [f["order_number"] for f in body["todays_fulfillments"]] == ["ORD-X"]
    assert _alert(body, "FULFILLMENT_OVERDUE") is None

    # 04:30 UTC == 00:30 EDT on 06-11 -> the same order is now past its date.
    freeze(datetime(2026, 6, 11, 4, 30, tzinfo=UTC))
    body = _dash(client)
    assert body["business_today"] == "2026-06-11"
    assert body["todays_fulfillments"] == []
    assert _alert(body, "FULFILLMENT_OVERDUE")["items"][0]["label"] == "ORD-X"


def test_dst_spring_forward_day_uses_the_correct_utc_offset_for_risk(client, session, freeze):
    """2026-03-08: New York springs forward at 02:00 local. A 12:00 local deadline is
    16:00 UTC (EDT, UTC-4) — not 17:00 UTC (EST, UTC-5)."""
    business = _setup(client, session)
    cookie = _cookie(session, business)
    dst_day = date(2026, 3, 8)
    confirm_demand(
        session,
        business,
        cookie,
        quantity="12",
        fulfillment_date=dst_day,
        fulfillment_time=time(12, 0),
        today=dst_day,
        order_number="ORD-DST",
    )
    # 15:30 UTC == 11:30 EDT: 30 minutes before the deadline, inside the 60-minute window.
    freeze(datetime(2026, 3, 8, 15, 30, tzinfo=UTC))
    assert _alert(_dash(client), "FULFILLMENT_AT_RISK")["items"][0]["label"] == "ORD-DST"
    # 14:30 UTC == 10:30 EDT: 90 minutes out -> not yet at risk.
    freeze(datetime(2026, 3, 8, 14, 30, tzinfo=UTC))
    assert _alert(_dash(client), "FULFILLMENT_AT_RISK") is None


def test_start_overdue_detail_is_truthful_for_both_trigger_branches(client, session, freeze):
    business = _setup(client, session)  # now = 08:00 local
    with_start = _cookie(session, business, name="Has Start", elapsed_time_minutes=120)
    no_start = _cookie(session, business, name="No Start")  # no elapsed time -> no suggested start
    _confirm(session, business, with_start, t=time(9, 0))  # suggested start 07:00 has passed
    _confirm(session, business, no_start, d=TODAY - timedelta(days=1))  # date passed, no time

    alert = _alert(_dash(client), "PRODUCTION_START_OVERDUE")
    details = {i["label"]: i["detail"] for i in alert["items"]}
    assert details["Has Start"] == "suggested start time has passed"
    assert (
        details["No Start"] == f"demand date {(TODAY - timedelta(days=1)).isoformat()} has passed"
    )
    assert "suggested start" not in details["No Start"]


def test_dashboard_reads_every_requirement_page_not_just_the_first(
    client, session, freeze, monkeypatch
):
    """Remediation F1: with a page size of 1, all actionable requirements must still reach
    Today's Production, the snapshot counts, and Upcoming — nothing past page one is lost."""
    from app.services import dashboard_service

    monkeypatch.setattr(dashboard_service, "REQUIREMENT_PAGE_SIZE", 1)
    business = _setup(client, session)
    names = ["Alpha", "Bravo", "Charlie"]
    for name in names:
        product = _cookie(session, business, name=name)
        _confirm(session, business, product, t=time(17, 0))
    later = _cookie(session, business, name="Delta")
    _confirm(session, business, later, d=TODAY + timedelta(days=2))

    body = _dash(client)
    assert {i["product_name"] for i in body["todays_production"]["items"]} == set(names)
    assert body["todays_snapshot"]["production_items_today"] == 3
    assert body["todays_snapshot"]["planned_batches_today"] == 3
    assert body["upcoming"][0]["production_item_count"] == 1
