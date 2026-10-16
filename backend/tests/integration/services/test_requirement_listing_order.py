"""Phase 9 final paging remediation: the Phase 8 requirement reader now has a deterministic
total order (`demand_date`, `id`), so OFFSET paging can neither repeat nor omit requirements
that share a demand date — and the Dashboard's `load_all_pages` returns every one of them."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.db.enums import ProductType
from app.services import production_run_service
from app.services.dashboard_service import load_all_pages
from tests.integration.schema.factories import (
    make_business_graph,
    make_product,
    make_production_requirement,
    make_recipe,
    make_recipe_revision,
)

_DAY = date(2026, 6, 10)


def _requirements(session: Session, business, *, same_date: int, other_dates: int = 3):
    ids = []
    for index in range(same_date + other_dates):
        product = make_product(session, business, product_type=ProductType.PRODUCED)
        recipe = make_recipe(session, business, product)
        revision = make_recipe_revision(session, business, recipe)
        # Many requirements deliberately share one demand date (ties); a few differ.
        demand_date = _DAY if index < same_date else _DAY + timedelta(days=index)
        ids.append(
            make_production_requirement(
                session, business, product, revision, demand_date=demand_date
            ).id
        )
    session.flush()
    return set(ids)


def test_reader_orders_by_demand_date_then_id(session: Session):
    business = make_business_graph(session)
    _requirements(session, business, same_date=8)
    items, total = production_run_service.list_production_requirements_for_business(
        session, business, limit=50, offset=0
    )
    keys = [(r.demand_date, r.id) for r in items]
    assert total == len(items) == 11
    assert keys == sorted(keys)


def test_offset_pages_of_tied_requirements_are_disjoint_and_complete(session: Session):
    business = make_business_graph(session)
    expected = _requirements(session, business, same_date=9)

    seen: list = []
    for offset in range(0, 12, 2):  # page size 2 forces ties across many page boundaries
        page, _total = production_run_service.list_production_requirements_for_business(
            session, business, limit=2, offset=offset
        )
        seen.extend(r.id for r in page)
    assert len(seen) == len(set(seen)), "a tied row was repeated across a page boundary"
    assert set(seen) == expected, "a tied row was omitted at a page boundary"


def test_load_all_pages_over_the_real_reader_returns_every_requirement_once(session: Session):
    business = make_business_graph(session)
    expected = _requirements(session, business, same_date=13)

    for page_size in (1, 2, 3, 5, 16, 100):  # includes exact multiples and one-past
        loaded = load_all_pages(
            lambda limit, offset: production_run_service.list_production_requirements_for_business(
                session, business, limit=limit, offset=offset
            ),
            key=lambda r: r.id,
            page_size=page_size,
        )
        keys = [(r.demand_date, r.id) for r in loaded]
        assert keys == sorted(keys)
        assert {r.id for r in loaded} == expected
        assert len(loaded) == len(expected) == len({r.id for r in loaded})
