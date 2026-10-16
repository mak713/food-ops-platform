"""Unit tests for the pure Dashboard classification/aggregation calculators (Phase 9)."""

from __future__ import annotations

import decimal
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.domain import dashboard_alerts as da

TZ = "America/New_York"
TODAY = date(2026, 6, 10)


def _utc(local: datetime) -> datetime:
    """Interpret a naive datetime as New York wall-clock and convert to UTC."""
    from zoneinfo import ZoneInfo

    return local.replace(tzinfo=ZoneInfo(TZ)).astimezone(UTC)


# ---------------------------------------------------------------- fulfillment timing (U1/U2)


def _classify(*, is_ready=False, fdate=TODAY, ftime=time(15, 0), now_local, warning=60):
    return da.classify_fulfillment(
        is_ready=is_ready,
        fulfillment_date=fdate,
        fulfillment_time=ftime,
        now_utc=_utc(now_local),
        business_today=TODAY,
        warning_minutes=warning,
        business_timezone=TZ,
    )


def test_at_risk_exactly_at_window_boundary_is_inclusive():
    # deadline 15:00, window 60 -> risk begins exactly at 14:00
    assert _classify(now_local=datetime(2026, 6, 10, 14, 0)) is da.FulfillmentTiming.AT_RISK


def test_one_minute_before_window_is_on_track():
    assert _classify(now_local=datetime(2026, 6, 10, 13, 59)) is da.FulfillmentTiming.ON_TRACK


def test_one_minute_inside_window_is_at_risk():
    assert _classify(now_local=datetime(2026, 6, 10, 14, 1)) is da.FulfillmentTiming.AT_RISK


def test_deadline_already_passed_today_is_still_at_risk_not_overdue():
    assert _classify(now_local=datetime(2026, 6, 10, 16, 0)) is da.FulfillmentTiming.AT_RISK


def test_ready_order_is_never_at_risk():
    assert (
        _classify(is_ready=True, now_local=datetime(2026, 6, 10, 14, 30))
        is da.FulfillmentTiming.ON_TRACK
    )


def test_past_date_is_overdue_for_confirmed_and_ready_alike():
    yesterday = TODAY - timedelta(days=1)
    now = datetime(2026, 6, 10, 9, 0)
    assert _classify(fdate=yesterday, now_local=now) is da.FulfillmentTiming.OVERDUE
    assert _classify(is_ready=True, fdate=yesterday, now_local=now) is da.FulfillmentTiming.OVERDUE


def test_date_only_order_never_gets_an_invented_deadline():
    # Even late in the evening of the fulfillment date, no time -> not at risk.
    assert (
        _classify(ftime=None, now_local=datetime(2026, 6, 10, 23, 59))
        is da.FulfillmentTiming.ON_TRACK
    )


def test_date_only_order_becomes_overdue_only_after_local_date_passes():
    assert (
        _classify(ftime=None, now_local=datetime(2026, 6, 10, 23, 59))
        is da.FulfillmentTiming.ON_TRACK
    )
    assert (
        _classify(
            ftime=None, fdate=TODAY - timedelta(days=1), now_local=datetime(2026, 6, 10, 0, 1)
        )
        is da.FulfillmentTiming.OVERDUE
    )


def test_future_order_far_outside_window_is_on_track():
    assert (
        _classify(fdate=TODAY + timedelta(days=3), now_local=datetime(2026, 6, 10, 12, 0))
        is da.FulfillmentTiming.ON_TRACK
    )


def test_deadline_is_none_for_nonexistent_dst_time():
    # 2026-03-08 02:30 does not exist in America/New_York (spring-forward gap).
    assert da.fulfillment_deadline_utc(date(2026, 3, 8), time(2, 30), TZ) is None


def test_deadline_is_none_for_ambiguous_dst_time():
    # 2026-11-01 01:30 occurs twice in America/New_York (fall-back overlap).
    assert da.fulfillment_deadline_utc(date(2026, 11, 1), time(1, 30), TZ) is None


def test_deadline_resolves_business_local_to_utc():
    deadline = da.fulfillment_deadline_utc(date(2026, 6, 10), time(15, 0), TZ)
    assert deadline == datetime(2026, 6, 10, 19, 0, tzinfo=UTC)


# ------------------------------------------------------------ production timing (U3)


def _timing(*, demand_date, suggested_start_at, now_local):
    return da.classify_production_timing(
        demand_date=demand_date,
        suggested_start_at=suggested_start_at,
        now_utc=_utc(now_local),
        business_today=TODAY,
        business_timezone=TZ,
    )


def test_advance_prep_when_suggested_start_today_for_later_fulfillment():
    start = _utc(datetime(2026, 6, 10, 18, 0))
    result = _timing(
        demand_date=TODAY + timedelta(days=1),
        suggested_start_at=start,
        now_local=datetime(2026, 6, 10, 9, 0),
    )
    assert result is da.ProductionTiming.ADVANCE_PREP


def test_due_today_when_start_and_demand_both_today():
    start = _utc(datetime(2026, 6, 10, 11, 0))
    result = _timing(
        demand_date=TODAY, suggested_start_at=start, now_local=datetime(2026, 6, 10, 9, 0)
    )
    assert result is da.ProductionTiming.DUE_TODAY


def test_start_overdue_when_suggested_start_already_passed():
    start = _utc(datetime(2026, 6, 10, 6, 0))
    result = _timing(
        demand_date=TODAY, suggested_start_at=start, now_local=datetime(2026, 6, 10, 9, 0)
    )
    assert result is da.ProductionTiming.START_OVERDUE


def test_suggested_start_crossing_into_prior_calendar_day_is_start_overdue_not_today():
    # Start was due yesterday evening for a fulfillment today.
    start = _utc(datetime(2026, 6, 9, 20, 0))
    result = _timing(
        demand_date=TODAY, suggested_start_at=start, now_local=datetime(2026, 6, 10, 0, 30)
    )
    assert result is da.ProductionTiming.START_OVERDUE


def test_future_start_is_not_today():
    start = _utc(datetime(2026, 6, 11, 6, 0))
    result = _timing(
        demand_date=TODAY + timedelta(days=1),
        suggested_start_at=start,
        now_local=datetime(2026, 6, 10, 9, 0),
    )
    assert result is da.ProductionTiming.NOT_TODAY


def test_null_start_is_date_level_only_never_fabricated():
    now = datetime(2026, 6, 10, 9, 0)
    assert (
        _timing(demand_date=TODAY, suggested_start_at=None, now_local=now)
        is da.ProductionTiming.DUE_TODAY
    )
    assert (
        _timing(demand_date=TODAY - timedelta(days=1), suggested_start_at=None, now_local=now)
        is da.ProductionTiming.START_OVERDUE
    )
    assert (
        _timing(demand_date=TODAY + timedelta(days=1), suggested_start_at=None, now_local=now)
        is da.ProductionTiming.NOT_TODAY
    )


def test_business_local_date_boundary_differs_from_utc_date():
    # 2026-06-10 23:30 New York is already 2026-06-11 03:30 UTC. A start at 23:45 local
    # on the 10th is still "today" for the business even though its UTC date is the 11th.
    start = _utc(datetime(2026, 6, 10, 23, 45))
    assert start.date() == date(2026, 6, 11)
    result = _timing(
        demand_date=TODAY + timedelta(days=1),
        suggested_start_at=start,
        now_local=datetime(2026, 6, 10, 23, 30),
    )
    assert result is da.ProductionTiming.ADVANCE_PREP


# ------------------------------------------------------------------ expiring surplus (U4)


def _expiring(**overrides):
    kwargs = dict(
        is_reusable=True,
        available_quantity=Decimal("2"),
        usable_through_date=TODAY,
        business_today=TODAY,
    )
    kwargs.update(overrides)
    return da.is_surplus_expiring(**kwargs)


def test_expiring_window_boundaries():
    assert _expiring(usable_through_date=TODAY) is True
    assert _expiring(usable_through_date=TODAY + timedelta(days=2)) is True
    assert _expiring(usable_through_date=TODAY + timedelta(days=3)) is False
    assert _expiring(usable_through_date=TODAY - timedelta(days=1)) is False


def test_expiring_excludes_undated_nonreusable_and_zero_available():
    assert _expiring(usable_through_date=None) is False
    assert _expiring(is_reusable=False) is False
    assert _expiring(available_quantity=Decimal("0")) is False


# ----------------------------------------------------------------- run progress


def test_run_progress_expected_duration_and_past_expected():
    started = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    progress = da.calculate_run_progress(
        started_at=started,
        now_utc=started + timedelta(minutes=130),
        planned_batches=2,
        elapsed_minutes_per_batch=60,
    )
    assert progress.elapsed_minutes == 130
    assert progress.expected_elapsed_minutes == 120
    assert progress.expected_finish_at == started + timedelta(minutes=120)
    assert progress.is_past_expected is True


def test_run_progress_without_elapsed_time_makes_no_expectation():
    started = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    progress = da.calculate_run_progress(
        started_at=started,
        now_utc=started + timedelta(minutes=5),
        planned_batches=1,
        elapsed_minutes_per_batch=None,
    )
    assert progress.expected_elapsed_minutes is None
    assert progress.expected_finish_at is None
    assert progress.is_past_expected is False


# ---------------------------------------------------------------------- aggregation (U5)


def _finding(code, key, label=None, **kw):
    return da.AlertFinding(code=code, key=key, label=label or key, **kw)


def test_same_code_findings_merge_into_one_alert_with_true_count():
    findings = [_finding(da.AlertCode.INGREDIENT_SHORTAGE, f"ing-{i}") for i in range(8)]
    alerts = da.aggregate_alerts(findings)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.count == 8
    assert len(alert.items) == da.ALERT_ITEM_CAP
    assert alert.items_truncated is True
    assert alert.title == "8 ingredients are short for upcoming demand"


def test_singular_title_and_no_truncation_flag_for_single_finding():
    alerts = da.aggregate_alerts([_finding(da.AlertCode.INGREDIENT_SHORTAGE, "a")])
    assert alerts[0].title == "1 ingredient is short for upcoming demand"
    assert alerts[0].items_truncated is False


def test_duplicate_subject_is_listed_once():
    findings = [
        _finding(da.AlertCode.FULFILLMENT_AT_RISK, "order-1", "ORD-1"),
        _finding(da.AlertCode.FULFILLMENT_AT_RISK, "order-1", "ORD-1"),
    ]
    alerts = da.aggregate_alerts(findings)
    assert alerts[0].count == 1


def test_warning_alerts_precede_notice_alerts_in_catalog_order():
    findings = [
        _finding(da.AlertCode.SURPLUS_EXPIRING, "lot"),
        _finding(da.AlertCode.PRODUCTION_START_OVERDUE, "req"),
        _finding(da.AlertCode.INGREDIENT_SHORTAGE, "ing"),
        _finding(da.AlertCode.FULFILLMENT_OVERDUE, "ord"),
    ]
    codes = [a.code for a in da.aggregate_alerts(findings)]
    assert codes == [
        da.AlertCode.FULFILLMENT_OVERDUE,
        da.AlertCode.INGREDIENT_SHORTAGE,
        da.AlertCode.PRODUCTION_START_OVERDUE,
        da.AlertCode.SURPLUS_EXPIRING,
    ]
    assert [a.severity for a in da.aggregate_alerts(findings)] == [
        da.AlertSeverity.WARNING,
        da.AlertSeverity.WARNING,
        da.AlertSeverity.NOTICE,
        da.AlertSeverity.NOTICE,
    ]


def test_no_findings_produces_no_alerts():
    assert da.aggregate_alerts([]) == []


def test_items_are_sorted_deterministically_regardless_of_input_order():
    a = [
        _finding(da.AlertCode.PRODUCT_MISSING_RECIPE, "b", "Bagels"),
        _finding(da.AlertCode.PRODUCT_MISSING_RECIPE, "a", "Apples"),
    ]
    forward = da.aggregate_alerts(a)
    backward = da.aggregate_alerts(list(reversed(a)))
    assert [i.label for i in forward[0].items] == ["Apples", "Bagels"]
    assert forward == backward


def test_every_alert_states_condition_impact_and_destination():
    # DASH-003: each alert describes the condition, impact, and actionable destination.
    findings = [_finding(code, "x") for code in da.AlertCode]
    for alert in da.aggregate_alerts(findings):
        assert alert.condition and alert.impact
        assert alert.destination_label and alert.destination_path.startswith("/app/")


# ----------------------------------------------------------------- Decimal context (U6)


def test_surplus_expiry_unaffected_by_corrupted_ambient_decimal_context():
    original = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 1
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        assert _expiring(available_quantity=Decimal("0.000001")) is True
    finally:
        decimal.setcontext(original)


@pytest.mark.parametrize("code", list(da.AlertCode))
def test_catalog_covers_every_alert_code(code):
    alerts = da.aggregate_alerts([_finding(code, "x")])
    assert alerts and alerts[0].code is code


# ------------------------------------------------- neutral alert wording (remediation F2)


def _spec_text(code: da.AlertCode) -> str:
    alert = da.aggregate_alerts([_finding(code, "x")])[0]
    return f"{alert.title} {alert.condition} {alert.impact}".lower()


def test_lost_ready_alert_makes_no_causal_claim_about_supply():
    text = _spec_text(da.AlertCode.ORDER_LOST_READY)
    assert "previously ready" in text and "no longer ready" in text
    for unsupported in ("supply", "reduced", "inventory", "surplus", "because"):
        assert unsupported not in text


def test_start_overdue_alert_covers_both_trigger_paths_without_implying_a_suggested_start():
    alert = da.aggregate_alerts([_finding(da.AlertCode.PRODUCTION_START_OVERDUE, "x")])[0]
    assert "suggested start time has passed" in alert.condition
    assert "demand date has passed" in alert.condition
    assert "no suggested start available" in alert.condition
