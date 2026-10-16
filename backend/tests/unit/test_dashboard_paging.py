"""Regression tests for the Dashboard's complete-read helper (Phase 9 remediation, Finding 1):
`load_all_pages` must exhaust the paginated Phase 8 reader and never silently discard later
pages (the former fixed `limit=10_000` ceiling)."""

from __future__ import annotations

import pytest

from app.core.api_errors import ApiError
from app.services.dashboard_service import load_all_pages


class _FakeReader:
    """A `(limit, offset) -> (items, total)` reader over a fixed list, recording calls."""

    def __init__(self, items: list[int], *, total: int | None = None) -> None:
        self.items = items
        self.total = len(items) if total is None else total
        self.calls: list[tuple[int, int]] = []

    def __call__(self, limit: int, offset: int):
        self.calls.append((limit, offset))
        return self.items[offset : offset + limit], self.total


def test_returns_every_item_across_many_pages_in_order():
    reader = _FakeReader(list(range(1, 1251)))
    result = load_all_pages(reader, key=lambda x: x, page_size=500)
    assert result == list(range(1, 1251))
    assert [offset for _limit, offset in reader.calls] == [0, 500, 1000]


def test_items_beyond_any_fixed_cap_are_not_discarded():
    # Far more rows than the old 10,000 ceiling — the helper must not stop at a cap.
    reader = _FakeReader(list(range(25_000)))
    assert len(load_all_pages(reader, key=lambda x: x, page_size=500)) == 25_000


def test_exact_multiple_of_page_size_terminates_on_the_empty_page():
    reader = _FakeReader(list(range(1000)))
    assert len(load_all_pages(reader, key=lambda x: x, page_size=500)) == 1000
    assert [offset for _l, offset in reader.calls] == [0, 500, 1000]


def test_empty_and_single_short_page():
    assert load_all_pages(_FakeReader([]), key=lambda x: x, page_size=500) == []
    assert load_all_pages(_FakeReader([7, 8]), key=lambda x: x, page_size=500) == [7, 8]


def test_a_transient_mismatch_is_recovered_by_the_bounded_reread():
    """A concurrent commit can make one pass inconsistent with the reported total; the
    whole read is repeated and the consistent pass is returned."""

    class Flaky(_FakeReader):
        def __init__(self) -> None:
            super().__init__(list(range(1, 11)))
            self.pass_number = 1

        def __call__(self, limit, offset):
            items, total = super().__call__(limit, offset)
            if self.pass_number == 1 and offset == 5:
                self.pass_number = 2
                return [items[0]] + items[:-1], total  # repeats a row, loses another
            return items, total

    reader = Flaky()
    result = load_all_pages(reader, key=lambda x: x, page_size=5)
    assert sorted(result) == list(range(1, 11))


def test_persistently_incomplete_read_is_rejected_not_returned_as_complete():
    reader = _FakeReader([1, 2, 3], total=5)  # reader claims 5 but only ever yields 3
    with pytest.raises(ApiError) as excinfo:
        load_all_pages(reader, key=lambda x: x, page_size=2)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "DASHBOARD_READ_INCONSISTENT"
    assert len(reader.calls) <= 3 * 2  # bounded: at most three passes of two pages


def test_stable_deterministic_reader_returns_each_item_exactly_once():
    reader = _FakeReader(list(range(100)))
    result = load_all_pages(reader, key=lambda x: x, page_size=7)
    assert result == list(range(100))
    assert len(result) == len(set(result))
