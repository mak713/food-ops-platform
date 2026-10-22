"""Phase 11 realistic account deletion (AUTH-008, AC-AUTH-008; release-blocking).

`DELETE /api/v1/account` is exercised over HTTP against a *full, interconnected* tenant graph
— customers, products/options, recipes + revisions, ingredients + inventory transactions,
purchased inventory + transactions, Orders with Lines/Payments/status history, requirements +
reservations, Production Runs + allocations, Surplus lots/allocations/transactions and all
five Order cost-ledger types — plus a second tenant that must be left byte-for-byte intact.
The earlier `test_account_deletion.py` only covered an empty tenant, and the schema cascade
test deletes with raw SQL; this proves the real ORM path (Business first, then User).
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models.auth import AuthSession
from app.db.models.business import Business
from app.db.models.user import User
from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup
from tests.integration.schema import factories as f

PASSWORD = "correct horse battery staple"


def build_full_graph(session: Session, business: Business) -> None:
    """Every kind of tenant-owned row, wired together (mirrors the Phase 1 cascade proof but
    attached to a Business created through the real signup flow)."""
    customer = f.make_customer(session, business)
    ingredient = f.make_ingredient(session, business)
    produced = f.make_product(session, business)
    session.flush()

    option = f.make_selling_option(session, business, produced)
    recipe = f.make_recipe(session, business, produced)
    session.flush()
    revision = f.make_recipe_revision(session, business, recipe)
    session.flush()
    f.make_recipe_revision_ingredient(session, business, revision, ingredient)
    f.make_inventory_transaction(session, business, ingredient)
    session.flush()

    order = f.make_order(session, business, status="CONFIRMED")
    order.customer_id = customer.id
    session.flush()
    line = f.make_order_line(
        session,
        business,
        order,
        line_type="STANDARD_OPTION",
        product=produced,
        selling_option=option,
    )
    session.flush()
    f.make_payment(session, business, order)
    f.make_order_status_history(session, business, order)
    session.flush()

    requirement = f.make_production_requirement(session, business, produced, revision)
    session.flush()
    f.make_production_requirement_order(session, business, requirement, order, line)
    f.make_production_ingredient_requirement(session, business, requirement, ingredient)
    f.make_ingredient_reservation(session, business, requirement, ingredient)
    session.flush()

    run = f.make_production_run(
        session, business, produced, revision, source_production_requirement_id=requirement.id
    )
    session.flush()
    f.make_production_run_ingredient(session, business, run, ingredient)
    allocation = f.make_production_run_order_allocation(session, business, run, order, line)
    session.flush()

    surplus = f.make_surplus_inventory(session, business, produced, source_production_run_id=run.id)
    session.flush()
    f.make_surplus_allocation(session, business, surplus, requirement, order, line)
    f.make_surplus_transaction(session, business, surplus)
    session.flush()

    purchased = f.make_product(session, business, product_type="PURCHASED")
    session.flush()
    f.make_purchased_inventory(session, business, purchased)
    purchased_txn = f.make_purchased_inventory_transaction(session, business, purchased)
    session.flush()
    f.make_purchased_product_reservation(session, business, purchased, order, line)
    session.flush()

    f.make_order_cost_allocation(
        session,
        business,
        order,
        cost_type="PRODUCTION",
        production_run_order_allocation_id=allocation.id,
    )
    f.make_order_cost_allocation(
        session, business, order, cost_type="SURPLUS", surplus_inventory_id=surplus.id
    )
    f.make_order_cost_allocation(
        session,
        business,
        order,
        cost_type="PURCHASED_GOOD",
        purchased_inventory_transaction_id=purchased_txn.id,
    )
    f.make_order_cost_allocation(session, business, order, cost_type="PACKAGING")
    f.make_order_cost_allocation(session, business, order, cost_type="CUSTOM_DIRECT")
    session.flush()


def tenant_tables() -> list[str]:
    """Every mapped table carrying a `business_id` column, discovered from metadata."""
    return sorted(
        t.name
        for t in Base.metadata.sorted_tables
        if "business_id" in t.columns and t.name != "businesses"
    )


def _fingerprint(session: Session, business_id) -> dict[str, tuple[int, str]]:
    session.expire_all()
    out = {}
    for table in tenant_tables():
        row = session.execute(
            text(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, ',' ORDER BY t::text)), '') "
                f"FROM {table} t WHERE business_id = :b"  # noqa: S608
            ),
            {"b": business_id},
        ).one()
        out[table] = (row[0], row[1])
    return out


def _signup(client, *, email: str, business_name: str) -> dict:
    return signup(client, email=email, password=PASSWORD, business_name=business_name)


def _delete(client, *, password=PASSWORD, name: str, headers=None):
    return client.request(
        "DELETE",
        "/api/v1/account",
        json={"current_password": password, "business_name_confirmation": name},
        headers=csrf_headers(client) if headers is None else headers,
    )


def test_api_deletion_removes_a_full_realistic_tenant_and_leaves_the_other_intact(
    client, session: Session
):
    a = _signup(client, email="del-a@example.com", business_name="Doomed Bakery")
    a_business = session.get(Business, a["business"]["id"])
    build_full_graph(session, a_business)
    f.make_password_reset_token(session, session.get(User, a["user"]["id"]))
    session.commit()
    a_cookies = {
        "fo_session": client.cookies.get("fo_session"),
        "fo_csrf": client.cookies.get("fo_csrf"),
    }

    client.cookies.clear()
    b = _signup(client, email="del-b@example.com", business_name="Survivor Bakery")
    b_business = session.get(Business, b["business"]["id"])
    build_full_graph(session, b_business)
    session.commit()
    b_cookies = {
        "fo_session": client.cookies.get("fo_session"),
        "fo_csrf": client.cookies.get("fo_csrf"),
    }

    a_id, b_id = a["business"]["id"], b["business"]["id"]
    assert all(count > 0 for count, _ in _fingerprint(session, a_id).values()), (
        "the realistic graph must populate every tenant-owned table"
    )
    b_before = _fingerprint(session, b_id)
    b_user_before = session.execute(
        text("select id::text, name, email, password_hash from users where id = :u"),
        {"u": b["user"]["id"]},
    ).one()

    def use(cookies):
        client.cookies.clear()
        for key, value in cookies.items():
            client.cookies.set(key, value)

    # Rejections leave the whole tenant intact.
    use(a_cookies)
    a_before = _fingerprint(session, a_id)
    wrong_password = _delete(client, password="not-the-password-0000", name="Doomed Bakery")
    assert wrong_password.status_code == 401
    wrong_name = _delete(client, name="Doomed bakery")
    assert wrong_name.status_code == 422
    no_csrf = _delete(client, name="Doomed Bakery", headers=ORIGIN_HEADERS)
    assert no_csrf.status_code == 403
    assert _fingerprint(session, a_id) == a_before

    # The real deletion.
    response = _delete(client, name="Doomed Bakery")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}
    set_cookie = " ".join(response.headers.get_list("set-cookie"))
    assert "fo_session=" in set_cookie and "fo_csrf=" in set_cookie  # cleared

    session.expire_all()
    for table, (count, _) in _fingerprint(session, a_id).items():
        assert count == 0, f"{table} still has rows scoped to the deleted business"
    assert session.get(Business, a_id) is None
    assert session.get(User, a["user"]["id"]) is None
    assert (
        session.execute(select(AuthSession).where(AuthSession.user_id == a["user"]["id"])).first()
        is None
    )
    assert (
        session.execute(
            text("select count(*) from password_reset_tokens where user_id = :u"),
            {"u": a["user"]["id"]},
        ).scalar_one()
        == 0
    )

    # The old session is dead; the other tenant is completely untouched and still works.
    use(a_cookies)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/settings").status_code == 401
    assert _fingerprint(session, b_id) == b_before
    assert (
        session.execute(
            text("select id::text, name, email, password_hash from users where id = :u"),
            {"u": b["user"]["id"]},
        ).one()
        == b_user_before
    )
    use(b_cookies)
    assert client.get("/api/v1/settings").json()["name"] == "Survivor Bakery"


def test_the_deleted_owner_can_no_longer_log_in_and_the_email_is_reusable(client, session):
    _signup(client, email="reuse@example.com", business_name="Gone Co")
    assert _delete(client, name="Gone Co").status_code == 200
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "reuse@example.com", "password": PASSWORD},
        headers=ORIGIN_HEADERS,
    )
    assert login.status_code == 401
    again = _signup(client, email="reuse@example.com", business_name="Fresh Co")
    assert again["business"]["name"] == "Fresh Co"
