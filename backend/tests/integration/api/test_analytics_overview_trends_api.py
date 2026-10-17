"""Phase 10 Overview + Trends API tests against the hand-computed `Golden` scenario
(AC-ANA-001..005, AC-ANA-008; Spec §13.2–13.4, §19.12–19.13)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.api.analytics_support import setup_golden

D = Decimal
OVERVIEW = "/api/v1/analytics/overview"
TRENDS = "/api/v1/analytics/trends"


@pytest.fixture()
def golden(client, session, monkeypatch):
    return setup_golden(client, session, monkeypatch)


def test_overview_last_7_days_kpis_with_comparison(client, golden):
    r = client.get(OVERVIEW, params={"range": "LAST_7_DAYS", "compare": "true"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["timezone"] == "America/Chicago"
    assert body["business_today"] == "2026-10-16"
    p = body["period"]
    assert (p["range"], p["start_date"], p["end_date"], p["days"]) == (
        "LAST_7_DAYS",
        "2026-10-10",
        "2026-10-16",
        7,
    )
    assert p["lower_bound_utc"] == "2026-10-10T05:00:00Z"
    assert p["upper_bound_utc"] == "2026-10-17T05:00:00Z"
    cp = body["comparison_period"]
    assert (cp["start_date"], cp["end_date"]) == ("2026-10-03", "2026-10-09")
    assert cp["upper_bound_utc"] == p["lower_bound_utc"]

    cur = body["current"]
    assert cur["revenue"] == "166.00"
    assert cur["direct_cost"] == "47.05"
    assert cur["contribution"] == "118.95"
    assert cur["contribution_margin"] == "0.716566"
    assert cur["completed_orders"] == 7
    assert cur["average_order_value"] == "23.71"

    prev = body["previous"]
    assert prev["revenue"] == "39.00"
    assert prev["direct_cost"] == "11.00"
    assert prev["contribution"] == "28.00"
    assert prev["contribution_margin"] == "0.717949"
    assert prev["completed_orders"] == 2
    assert prev["average_order_value"] == "19.50"
    assert body["previous_has_data"] is True

    d = body["deltas"]
    assert d["revenue"] == {"absolute": "127.00", "percent_change": "3.256410"}
    assert d["direct_cost"] == {"absolute": "36.05", "percent_change": "3.277273"}
    assert d["contribution"] == {"absolute": "90.95", "percent_change": "3.248214"}
    assert d["contribution_margin"] == {"absolute": "-0.001383"}
    assert d["completed_orders"] == {"absolute": 5, "percent_change": "2.500000"}
    assert d["average_order_value"] == {"absolute": "4.21", "percent_change": "0.215897"}


def test_overview_without_compare_has_no_previous_block(client, golden):
    body = client.get(OVERVIEW, params={"range": "LAST_7_DAYS"}).json()
    assert body["comparison_period"] is None
    assert body["previous"] is None
    assert body["deltas"] is None
    assert body["previous_has_data"] is None


def test_non_completed_and_out_of_period_orders_never_contaminate(client, golden):
    cur = client.get(OVERVIEW, params={"range": "LAST_7_DAYS"}).json()["current"]
    # Confirmed/Ready/Draft/Canceled Orders (incl. Canceled with a 99.00 PRODUCTION row) and E4
    # (Oct 17 05:00:00Z = next local day) are excluded: figures equal the Golden docstring.
    assert cur["revenue"] == "166.00"
    assert cur["direct_cost"] == "47.05"
    assert cur["completed_orders"] == 7


def test_boundary_instants_by_business_local_time(client, golden):
    def one_day(day):
        return client.get(
            OVERVIEW, params={"range": "CUSTOM", "start_date": day, "end_date": day}
        ).json()["current"]

    # E1 (Oct 17 04:59:59Z, local Oct 16 23:59:59) belongs to Oct 16 only.
    day16 = one_day("2026-10-16")
    assert day16["completed_orders"] == 2  # O5 + E1
    assert day16["revenue"] == "3.00"
    # E4 (Oct 17 05:00:00Z) is the first instant of local Oct 17.
    day17 = one_day("2026-10-17")
    assert (day17["completed_orders"], day17["revenue"]) == (1, "100.00")
    # E2 (Oct 10 05:00:00Z, local midnight) is in Oct 10; E3 (04:59:59Z) is in Oct 9.
    assert (one_day("2026-10-10")["completed_orders"], one_day("2026-10-10")["revenue"]) == (
        1,
        "4.00",
    )
    assert (one_day("2026-10-09")["completed_orders"], one_day("2026-10-09")["revenue"]) == (
        1,
        "9.00",
    )


def test_last_30_days_includes_previous_period_orders(client, golden):
    cur = client.get(OVERVIEW, params={"range": "LAST_30_DAYS"}).json()["current"]
    # Sep 17..Oct 16 also contains O6 (30.00 / 10.00) and E3 (9.00 / 1.00).
    assert cur["revenue"] == "205.00"
    assert cur["completed_orders"] == 9
    assert cur["direct_cost"] == "58.05"


def test_this_month_and_this_year_ranges(client, golden):
    month = client.get(OVERVIEW, params={"range": "THIS_MONTH", "compare": "true"}).json()
    assert month["period"]["start_date"] == "2026-10-01"
    assert month["period"]["end_date"] == "2026-10-16"
    assert month["comparison_period"]["start_date"] == "2026-09-15"
    assert month["comparison_period"]["end_date"] == "2026-09-30"
    assert month["current"]["revenue"] == "205.00"
    year = client.get(OVERVIEW, params={"range": "THIS_YEAR", "compare": "true"}).json()
    assert year["period"]["start_date"] == "2026-01-01"
    assert year["period"]["days"] == 289
    assert year["period"]["lower_bound_utc"] == "2026-01-01T06:00:00Z"
    assert year["comparison_period"]["start_date"] == "2025-03-18"
    assert year["comparison_period"]["end_date"] == "2025-12-31"
    assert year["previous"]["completed_orders"] == 0
    assert year["previous_has_data"] is False
    assert year["deltas"]["revenue"]["percent_change"] is None


def test_empty_period_returns_safe_zero_null_states(client, golden):
    body = client.get(
        OVERVIEW,
        params={
            "range": "CUSTOM",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "compare": "true",
        },
    ).json()
    cur = body["current"]
    assert cur["revenue"] == "0.00"
    assert cur["direct_cost"] == "0.00"
    assert cur["contribution"] == "0.00"
    assert cur["contribution_margin"] is None
    assert cur["completed_orders"] == 0
    assert cur["average_order_value"] is None
    assert body["previous_has_data"] is False
    assert body["deltas"]["revenue"] == {"absolute": "0.00", "percent_change": None}
    assert body["deltas"]["contribution_margin"] == {"absolute": None}


# ------------------------------------------------------------------ Trends


def test_daily_trends_zero_filled_and_sum_to_overview(client, golden):
    body = client.get(TRENDS, params={"range": "LAST_7_DAYS"}).json()
    assert body["granularity"] == "DAY"
    labels = [b["label"] for b in body["buckets"]]
    assert labels == [f"2026-10-{d}" for d in range(10, 17)]
    by = {b["label"]: b for b in body["buckets"]}
    # Oct 10: E2 (4.00/1.00). Oct 12 local: O1 (15:00Z = 10:00 CDT) + O2 (Oct 13 01:30Z = Oct 12
    # 20:30 CDT) = 46+72 / 12.75+21.50. Oct 14: O3. Oct 15: O4. Oct 16: O5 + E1 (23:59:59 local).
    assert (by["2026-10-10"]["revenue"], by["2026-10-10"]["completed_orders"]) == ("4.00", 1)
    assert (by["2026-10-11"]["revenue"], by["2026-10-11"]["completed_orders"]) == ("0.00", 0)
    assert (by["2026-10-12"]["revenue"], by["2026-10-12"]["direct_cost"]) == ("118.00", "34.25")
    assert by["2026-10-12"]["contribution"] == "83.75"
    assert by["2026-10-12"]["completed_orders"] == 2
    assert by["2026-10-13"]["completed_orders"] == 0
    assert by["2026-10-14"]["revenue"] == "17.00"
    assert by["2026-10-15"]["revenue"] == "24.00"
    assert (by["2026-10-16"]["revenue"], by["2026-10-16"]["completed_orders"]) == ("3.00", 2)

    overview = client.get(OVERVIEW, params={"range": "LAST_7_DAYS"}).json()["current"]
    totals = body["totals"]
    assert totals["revenue"] == overview["revenue"] == "166.00"
    assert totals["direct_cost"] == overview["direct_cost"]
    assert totals["contribution"] == overview["contribution"]
    assert totals["completed_orders"] == overview["completed_orders"]
    assert sum(D(b["revenue"]) for b in body["buckets"]) == D(overview["revenue"])


def test_weekly_and_monthly_granularity(client, golden):
    weekly = client.get(TRENDS, params={"range": "LAST_90_DAYS"}).json()
    assert weekly["granularity"] == "WEEK"
    first, last = weekly["buckets"][0], weekly["buckets"][-1]
    assert (first["start_date"], first["end_date"]) == ("2026-07-19", "2026-07-19")
    assert (last["start_date"], last["end_date"]) == ("2026-10-12", "2026-10-16")
    assert sum(D(b["revenue"]) for b in weekly["buckets"]) == D(weekly["totals"]["revenue"])

    monthly = client.get(TRENDS, params={"range": "THIS_YEAR"}).json()
    assert monthly["granularity"] == "MONTH"
    assert [b["label"] for b in monthly["buckets"]] == [f"2026-{m:02d}" for m in range(1, 11)]
    october = monthly["buckets"][-1]
    assert october["revenue"] == "205.00"
    assert monthly["buckets"][0]["completed_orders"] == 0


def test_trends_for_empty_period_are_all_zero(client, golden):
    body = client.get(
        TRENDS,
        params={"range": "CUSTOM", "start_date": "2025-01-01", "end_date": "2025-01-05"},
    ).json()
    assert len(body["buckets"]) == 5
    assert all(b["revenue"] == "0.00" and b["completed_orders"] == 0 for b in body["buckets"])
    assert body["totals"]["contribution_margin"] is None
