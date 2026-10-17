"""Phase 10 Business-local reporting-boundary tests, end to end through the API (Spec §19.13).

One Completed Order per hour is inserted across a local-midnight/DST transition. For every
local calendar day in the window a single-day report must count exactly the Orders whose
Business-local civil date is that day (an independent oracle: Python's own
`astimezone(tz).date()`), the days must partition the window (no instant counted twice or
dropped), and the multi-day report must equal the sum of its days — including zones whose
transition falls on, or skips, local midnight.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.integration.api.analytics_support import Builder, custom_item, freeze
from tests.integration.api.helpers import signup
from tests.integration.api.phase9_support import business_by_name

OVERVIEW = "/api/v1/analytics/overview"

CASES = [
    # (zone, first local day, number of days, note)
    ("America/Chicago", date(2026, 3, 7), 4, "spring-forward 02:00"),
    ("America/Chicago", date(2026, 10, 31), 4, "fall-back 02:00 (25 h day)"),
    ("America/Sao_Paulo", date(2018, 11, 2), 5, "nonexistent midnight (00:00 -> 01:00)"),
    ("America/Havana", date(2020, 3, 7), 4, "nonexistent midnight (spring-forward)"),
    ("America/Havana", date(2020, 10, 31), 4, "ambiguous midnight (fall-back)"),
    ("Pacific/Apia", date(2011, 12, 28), 5, "skipped civil date 2011-12-30"),
    ("Asia/Kolkata", date(2026, 10, 14), 4, "half-hour offset"),
    ("Pacific/Auckland", date(2026, 9, 25), 5, "ahead of UTC, DST start"),
]


@pytest.mark.parametrize(("zone", "first_day", "days", "note"), CASES)
def test_single_day_reports_partition_time_exactly(
    client, session, monkeypatch, zone, first_day, days, note
):
    tz = ZoneInfo(zone)
    signup(client, business_timezone=zone)
    business = business_by_name(session)
    freeze(monkeypatch, datetime(2027, 1, 15, 12, tzinfo=UTC))

    # Window: from 2 h before the first local midnight to 2 h after the last.
    start_utc = datetime.combine(first_day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    end_day = first_day + timedelta(days=days)
    end_utc = datetime.combine(end_day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    t = start_utc - timedelta(hours=2)
    builder = Builder(session, business)
    instants: list[datetime] = []
    while t < end_utc + timedelta(hours=2):
        builder.order(completed_at=t, number=f"T{len(instants)}", lines=[custom_item("1.00")])
        instants.append(t)
        t += timedelta(hours=1)
    session.commit()

    def count(start: date, end: date) -> int:
        body = client.get(
            OVERVIEW, params={"range": "CUSTOM", "start_date": start, "end_date": end}
        ).json()
        return body["current"]["completed_orders"]

    per_day = {}
    for i in range(days):
        day = first_day + timedelta(days=i)
        per_day[day] = count(day, day)
        expected = sum(1 for x in instants if x.astimezone(tz).date() == day)
        assert per_day[day] == expected, (zone, note, day)

    window_total = count(first_day, first_day + timedelta(days=days - 1))
    assert window_total == sum(per_day.values())
    assert window_total == sum(
        1 for x in instants if first_day <= x.astimezone(tz).date() < end_day
    )


def test_skipped_civil_date_reports_zero_orders_and_neighbours_keep_theirs(
    client, session, monkeypatch
):
    zone = "Pacific/Apia"
    tz = ZoneInfo(zone)
    signup(client, business_timezone=zone)
    business = business_by_name(session)
    freeze(monkeypatch, datetime(2012, 1, 15, 12, tzinfo=UTC))
    builder = Builder(session, business)
    # 2011-12-29 23:59:59 local (UTC-10) and the first instant of 2011-12-31 local (UTC+14).
    before = datetime(2011, 12, 30, 9, 59, 59, tzinfo=UTC)
    after = datetime(2011, 12, 30, 10, 0, 0, tzinfo=UTC)
    assert before.astimezone(tz).date() == date(2011, 12, 29)
    assert after.astimezone(tz).date() == date(2011, 12, 31)
    builder.order(completed_at=before, number="B", lines=[custom_item("2.00")])
    builder.order(completed_at=after, number="A", lines=[custom_item("3.00")])
    session.commit()

    def one(day: str):
        return client.get(
            OVERVIEW, params={"range": "CUSTOM", "start_date": day, "end_date": day}
        ).json()

    skipped = one("2011-12-30")
    assert skipped["current"]["completed_orders"] == 0
    assert skipped["period"]["lower_bound_utc"] == skipped["period"]["upper_bound_utc"]
    assert one("2011-12-29")["current"]["revenue"] == "2.00"
    assert one("2011-12-31")["current"]["revenue"] == "3.00"
    assert one("2011-12-29")["period"]["upper_bound_utc"] == after.isoformat().replace(
        "+00:00", "Z"
    )
