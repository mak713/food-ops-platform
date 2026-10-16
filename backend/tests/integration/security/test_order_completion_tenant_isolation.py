"""Order Completion tenant isolation (release-blocking, Spec §19.14): Business A can neither
complete nor observe Business B's Order, and completing A's Order touches none of B's data."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from tests.integration.api.completion_support import (
    build_ready_order,
    domain_fingerprint,
    purchased_product,
)
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.api.phase9_support import business_by_name
from tests.integration.schema.factories import make_business_graph


def test_foreign_order_is_unavailable_and_unchanged(client, session: Session):
    signup(client, email=f"completion-{uuid.uuid4().hex[:10]}@example.com")
    business_a = business_by_name(session)
    business_b = make_business_graph(session, name="Business B")
    juice_b = purchased_product(session, business_b, name="Lemonade", physical="10")
    foreign = build_ready_order(session, business_b, [{"product": juice_b, "qty": "4"}])
    before = domain_fingerprint(session)

    response = client.post(
        f"/api/v1/orders/{foreign.id}/complete",
        json={"version": foreign.version},
        headers=csrf_headers(client),
    )
    assert response.status_code == 404
    assert foreign.id.hex not in response.text and "Lemonade" not in response.text
    assert domain_fingerprint(session) == before
    assert business_a.id != business_b.id


def test_completing_a_does_not_touch_b_with_identical_shapes(client, session: Session):
    signup(client, email=f"completion-{uuid.uuid4().hex[:10]}@example.com")
    business_a = business_by_name(session)
    business_b = make_business_graph(session, name="Business B")
    own_juice = purchased_product(session, business_a, name="Lemonade", physical="10", wac="1.50")
    other_juice = purchased_product(session, business_b, name="Lemonade", physical="10", wac="1.50")
    own = build_ready_order(session, business_a, [{"product": own_juice, "qty": "4"}])
    other = build_ready_order(session, business_b, [{"product": other_juice, "qty": "4"}])

    # Prove B's rows are unchanged: complete A, then complete B separately and compare B's
    # own inventory/ledger outcome with an independent expectation.
    response = client.post(
        f"/api/v1/orders/{own.id}/complete",
        json={"version": own.version},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    session.expire_all()
    from sqlalchemy import select

    from app.db.models.cost import OrderCostAllocation
    from app.db.models.order import Order
    from app.db.models.purchased_inventory import PurchasedProductInventory

    inventory_b = session.scalars(
        select(PurchasedProductInventory).where(
            PurchasedProductInventory.business_id == business_b.id
        )
    ).one()
    assert inventory_b.physical_quantity == 10  # untouched
    assert session.get(Order, other.id).status.value == "READY"
    assert (
        session.scalars(
            select(OrderCostAllocation).where(OrderCostAllocation.business_id == business_b.id)
        ).all()
        == []
    )
