"""Phase 10 query-validation / auth / contract tests (Spec §11.9, §17.12; AC-ANA-001)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.integration.api.analytics_support import setup_golden
from tests.integration.api.helpers import signup

BASE = "/api/v1/analytics"
JSON_ENDPOINTS = ["overview", "trends", "products", "customers", "orders"]
CSV_ENDPOINTS = ["products.csv", "customers.csv", "orders.csv", "trends.csv"]


@pytest.fixture()
def golden(client, session, monkeypatch):
    return setup_golden(client, session, monkeypatch)


def _codes(response) -> list[str]:
    return [i["code"] for i in response.json()["error"]["issues"]]


@pytest.mark.parametrize("endpoint", JSON_ENDPOINTS + CSV_ENDPOINTS)
def test_unauthenticated_requests_are_rejected(client, endpoint):
    response = client.get(f"{BASE}/{endpoint}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_UNAUTHENTICATED"


@pytest.mark.parametrize("endpoint", JSON_ENDPOINTS)
def test_default_range_is_last_30_days(client, golden, endpoint):
    response = client.get(f"{BASE}/{endpoint}")
    assert response.status_code == 200, response.text
    assert response.json()["period"]["range"] == "LAST_30_DAYS"
    assert response.json()["period"]["days"] == 30


@pytest.mark.parametrize("endpoint", JSON_ENDPOINTS + CSV_ENDPOINTS)
def test_unknown_range_is_a_structured_422(client, golden, endpoint):
    response = client.get(f"{BASE}/{endpoint}", params={"range": "LAST_5_DAYS"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_malformed_dates_are_a_structured_422(client, golden):
    response = client.get(
        f"{BASE}/overview",
        params={"range": "CUSTOM", "start_date": "2026-13-45", "end_date": "2026-10-16"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["issues"][0]["field"].endswith("start_date")


def test_custom_requires_both_dates(client, golden):
    response = client.get(f"{BASE}/overview", params={"range": "CUSTOM"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "ANALYTICS_RANGE_INVALID"
    assert set(_codes(response)) == {
        "ANALYTICS_CUSTOM_START_REQUIRED",
        "ANALYTICS_CUSTOM_END_REQUIRED",
    }
    only_start = client.get(
        f"{BASE}/trends", params={"range": "CUSTOM", "start_date": "2026-10-01"}
    )
    assert only_start.status_code == 422
    assert _codes(only_start) == ["ANALYTICS_CUSTOM_END_REQUIRED"]


def test_start_after_end_is_rejected(client, golden):
    response = client.get(
        f"{BASE}/products",
        params={"range": "CUSTOM", "start_date": "2026-10-16", "end_date": "2026-10-15"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ANALYTICS_RANGE_INVALID"
    issue = response.json()["error"]["issues"][0]
    assert issue["code"] == "ANALYTICS_RANGE_START_AFTER_END"
    assert issue["field"] == "start_date"
    assert issue["severity"] == "ERROR"


@pytest.mark.parametrize(
    "range_", ["LAST_7_DAYS", "LAST_30_DAYS", "LAST_90_DAYS", "THIS_MONTH", "THIS_YEAR"]
)
def test_presets_reject_supplied_custom_dates(client, golden, range_):
    response = client.get(f"{BASE}/overview", params={"range": range_, "end_date": "2026-10-01"})
    assert response.status_code == 422
    assert _codes(response) == ["ANALYTICS_PRESET_DATES_NOT_ALLOWED"]


def test_custom_range_of_exactly_3660_days_is_accepted_everywhere(client, golden):
    end = date(2026, 10, 16)
    start = end - timedelta(days=3659)
    params = {"range": "CUSTOM", "start_date": start.isoformat(), "end_date": end.isoformat()}
    for endpoint in JSON_ENDPOINTS + CSV_ENDPOINTS:
        response = client.get(f"{BASE}/{endpoint}", params=params)
        assert response.status_code == 200, (endpoint, response.text[:200])
    overview = client.get(f"{BASE}/overview", params=params).json()
    assert overview["period"]["days"] == 3660
    assert overview["current"]["completed_orders"] == 9
    trends = client.get(f"{BASE}/trends", params=params).json()
    assert trends["granularity"] == "MONTH"
    months = (end.year * 12 + end.month) - (start.year * 12 + start.month) + 1
    assert len(trends["buckets"]) == months  # complete, zero-filled — never truncated
    assert trends["totals"]["revenue"] == overview["current"]["revenue"]


def test_custom_range_of_3661_days_is_a_structured_422_on_every_surface(client, golden):
    end = date(2026, 10, 16)
    start = end - timedelta(days=3660)
    params = {"range": "CUSTOM", "start_date": start.isoformat(), "end_date": end.isoformat()}
    for endpoint in JSON_ENDPOINTS + CSV_ENDPOINTS:
        response = client.get(f"{BASE}/{endpoint}", params=params)
        assert response.status_code == 422, endpoint
        assert response.headers["content-type"].startswith("application/json")
        error = response.json()["error"]
        assert error["code"] == "ANALYTICS_RANGE_INVALID"
        issue = error["issues"][0]
        assert issue["code"] == "ANALYTICS_RANGE_TOO_LARGE"
        assert issue["severity"] == "ERROR" and issue["field"] == "end_date"
        assert issue["details"] == {"requested_days": 3661, "maximum_days": 3660}
        # No partial/truncated report body of any kind accompanies the rejection.
        assert not {"buckets", "rows", "items", "current"} & set(response.json())
        assert not response.content.startswith(b"\xef\xbb\xbf")


def test_a_gigantic_range_is_rejected_by_size_quickly(client, golden):
    response = client.get(
        f"{BASE}/trends",
        params={"range": "CUSTOM", "start_date": "0001-01-05", "end_date": "9999-12-30"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "ANALYTICS_RANGE_TOO_LARGE"
    assert len(response.content) < 2000


def test_the_limit_is_unrelated_to_the_shopping_list_90_day_rule(client, golden):
    # Analytics accepts a 120-day Custom Range ...
    analytics = client.get(
        f"{BASE}/overview",
        params={"range": "CUSTOM", "start_date": "2026-06-01", "end_date": "2026-09-28"},
    )
    assert analytics.status_code == 200 and analytics.json()["period"]["days"] == 120
    # ... while the frozen Shopping List still rejects anything over 90 inclusive days.
    today = datetime.now(UTC).date() + timedelta(days=2)
    shopping = client.get(
        "/api/v1/shopping-list",
        params={
            "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=90)).isoformat(),
        },
    )
    assert shopping.status_code == 422
    assert shopping.json()["error"]["code"] == "SHOPPING_LIST_RANGE_INVALID"


def test_unrepresentable_previous_period_is_422_not_clamped(client, golden):
    params = {"range": "CUSTOM", "start_date": "0001-01-05", "end_date": "0001-01-20"}
    ok = client.get(f"{BASE}/overview", params=params)
    # Without comparison the (extreme but representable) range is served.
    assert ok.status_code == 200, ok.text
    bad = client.get(f"{BASE}/overview", params={**params, "compare": "true"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "ANALYTICS_RANGE_INVALID"
    issue = bad.json()["error"]["issues"][0]
    assert issue["code"] == "ANALYTICS_PREVIOUS_PERIOD_NOT_REPRESENTABLE"
    assert issue["field"] == "compare"


def test_bad_sort_direction_and_paging_params_are_422(client, golden):
    assert client.get(f"{BASE}/products", params={"sort": "bogus"}).status_code == 422
    assert client.get(f"{BASE}/customers", params={"direction": "up"}).status_code == 422
    assert client.get(f"{BASE}/customers", params={"limit": 0}).status_code == 422
    assert client.get(f"{BASE}/orders", params={"limit": 201}).status_code == 422
    assert client.get(f"{BASE}/orders", params={"offset": -1}).status_code == 422
    assert client.get(f"{BASE}/orders", params={"status": "DRAFT"}).status_code == 422


def test_client_supplied_business_id_is_ignored(client, golden, session):
    other = client.get(
        f"{BASE}/overview",
        params={"range": "LAST_7_DAYS", "business_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert other.status_code == 200
    assert other.json()["current"]["revenue"] == "166.00"


@pytest.mark.parametrize("endpoint", JSON_ENDPOINTS + CSV_ENDPOINTS)
def test_endpoints_are_read_only(client, golden, endpoint):
    from tests.integration.api.helpers import csrf_headers

    response = client.post(f"{BASE}/{endpoint}", headers=csrf_headers(client))
    assert response.status_code in (404, 405)


def test_a_second_business_with_another_timezone_uses_its_own_calendar(
    client, session, monkeypatch
):
    # Asia/Kolkata (+05:30): Oct 16 18:00Z is already Oct 16 23:30 local.
    from tests.integration.api.analytics_support import freeze

    signup(client, business_timezone="Asia/Kolkata")
    freeze(monkeypatch)
    body = client.get(f"{BASE}/overview", params={"range": "LAST_7_DAYS"}).json()
    assert body["timezone"] == "Asia/Kolkata"
    assert body["business_today"] == "2026-10-16"
    assert body["period"]["lower_bound_utc"] == "2026-10-09T18:30:00Z"
    assert body["period"]["upper_bound_utc"] == "2026-10-16T18:30:00Z"
