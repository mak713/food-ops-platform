"""Inventory concurrency/race tests (Phase 5 Plan §G; approved decision 4's required
race coverage): the Initial-Balance/first-Restock creation races for both Ingredient and
Purchased Product Inventory, proving the shared-ancestor row lock (ADR-106) correctly
serializes every combination rather than relying on the database's own `UNIQUE`
constraint as the primary mechanism.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient, InventoryTransaction
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.user import User
from app.schemas.inventory import (
    IngredientInitialBalanceRequest,
    IngredientRestockRequest,
    PurchasedInitialBalanceRequest,
    PurchasedRestockRequest,
)
from app.services import ingredient_inventory_service, purchased_inventory_service
from tests.integration.schema.factories import make_business_graph, make_ingredient, make_product


def _run_symmetric_lock_race(monkeypatch, module, attr_name, thread_bodies):
    """Both `thread_bodies` ultimately call through the exact same real locked-fetch
    function (`module.attr_name`) — wraps it so both callers are forced to reach the
    wrapper (via `entry_barrier`) before either attempts the real `SELECT ... FOR UPDATE`,
    guaranteeing genuine contention on the same row rather than merely hoping thread
    start-order produces it. Whichever caller's real call returns first (i.e. genuinely
    won the Postgres lock) holds its transaction open — deliberately not yet returning to
    its own caller's insert/commit logic — until the test confirms a winner exists, so the
    loser's own call is provably still blocked on the same row when the winner is released.
    """
    entry_barrier = threading.Barrier(2)
    winner_confirmed_event = threading.Event()
    release_gate = threading.Event()
    winner_lock = threading.Lock()
    state = {"claimed": False}

    real_fn = getattr(module, attr_name)

    def wrapper(*args, **kwargs):
        entry_barrier.wait(timeout=5)
        result = real_fn(*args, **kwargs)
        with winner_lock:
            is_winner = not state["claimed"]
            state["claimed"] = True
        if is_winner:
            winner_confirmed_event.set()
            release_gate.wait(timeout=5)
        return result

    monkeypatch.setattr(module, attr_name, wrapper)

    results: list[tuple[str, object]] = []
    threads = [threading.Thread(target=body, args=(results,)) for body in thread_bodies]
    for t in threads:
        t.start()
    assert winner_confirmed_event.wait(timeout=5), "neither thread ever acquired the lock"
    release_gate.set()
    for t in threads:
        t.join(timeout=10)
    return results


def _cleanup(real_session_factory, *, business_id, extra_rows=()):
    cleanup = real_session_factory()
    try:
        for row in extra_rows:
            fresh = cleanup.get(type(row), row.id)
            if fresh is not None:
                cleanup.delete(fresh)
                cleanup.flush()
        business = cleanup.get(Business, business_id)
        if business is not None:
            owner_id = business.owner_user_id
            cleanup.delete(business)
            cleanup.flush()
            user = cleanup.get(User, owner_id)
            if user is not None:
                cleanup.delete(user)
        cleanup.commit()
    finally:
        cleanup.close()


# --- Ingredient: two concurrent Initial-Balance attempts --------------------------------


def test_two_concurrent_ingredient_initial_balances_exactly_one_wins(
    real_session_factory, monkeypatch
):
    """Both attempts submit the ingredient's real starting `version` (1). The winner's
    commit both inserts the transaction row *and* bumps `version` to 2 as one atomic unit
    (`version_id_col` on the Ingredient row's own `UPDATE`) — so by the time the loser's
    lock unblocks, `check_version` (which runs *before* the already-initialized check,
    matching the approved ordering) always finds `version == 2 != 1` and rejects with
    `STALE_VERSION` first. The already-initialized rejection is still reachable — just not
    via this exact race shape (a client that re-reads the bumped version and only then
    retries reaches it instead; already covered by `test_ingredient_inventory.py::
    test_initial_balance_rejects_a_second_attempt`) — this test's job is only to prove no
    lost update/no double-initialization under real concurrency, which `STALE_VERSION`
    already guarantees here."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()
        business_id, ingredient_id = business.id, ingredient.id
    finally:
        setup_db.close()

    def attempt(quantity, results):
        db = real_session_factory()
        try:
            business_row = db.get(Business, business_id)
            payload = IngredientInitialBalanceRequest(
                version=1, quantity=quantity, unit="g", unit_cost="1"
            )
            ingredient_inventory_service.create_initial_balance(
                db, business_row, ingredient_id, payload
            )
            results.append(("ok", quantity))
        except ApiError as exc:
            db.rollback()
            results.append(("api_error", exc))
        finally:
            db.close()

    try:
        results = _run_symmetric_lock_race(
            monkeypatch,
            ingredient_inventory_service,
            "get_ingredient_for_business_locked",
            [lambda r: attempt("100", r), lambda r: attempt("200", r)],
        )
        oks = [r for kind, r in results if kind == "ok"]
        errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, results
        assert len(errors) == 1, results
        assert errors[0].code == "STALE_VERSION"

        verify_db = real_session_factory()
        try:
            final_ingredient = verify_db.get(Ingredient, ingredient_id)
            txn_count = (
                verify_db.query(InventoryTransaction)
                .filter(InventoryTransaction.ingredient_id == ingredient_id)
                .count()
            )
            assert txn_count == 1, "the loser must never have inserted a second transaction"
            assert final_ingredient.physical_quantity == Decimal(oks[0])
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- Ingredient: Initial Balance racing a concurrent Restock -----------------------------


def test_ingredient_initial_balance_racing_a_concurrent_restock_never_loses_an_update(
    real_session_factory,
):
    """Restock never locks — this proves the interleaving is still safe purely from
    combining the Initial-Balance lock with `version_id_col`: Restock either reads the
    ingredient before Initial Balance commits (and later fails at `commit_or_raise_stale`
    once its blocked `UPDATE` finally runs against a since-bumped version) or reads it
    after (and fails synchronously at `check_version`) — every interleaving ends the same
    way: Initial Balance always succeeds, Restock always receives `409 STALE_VERSION`,
    and the final state is exactly Initial Balance's own values, never a lost update."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()
        business_id, ingredient_id = business.id, ingredient.id
    finally:
        setup_db.close()

    start_barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def _initial_balance():
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business_row = db.get(Business, business_id)
            payload = IngredientInitialBalanceRequest(
                version=1, quantity="100", unit="g", unit_cost="2"
            )
            ingredient_inventory_service.create_initial_balance(
                db, business_row, ingredient_id, payload
            )
            results.append(("initial_balance_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("initial_balance_error", exc))
        finally:
            db.close()

    def _restock():
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business_row = db.get(Business, business_id)
            payload = IngredientRestockRequest(version=1, quantity="1", unit="g", unit_cost="9")
            ingredient_inventory_service.restock_ingredient(
                db, business_row, ingredient_id, payload
            )
            results.append(("restock_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("restock_error", exc))
        finally:
            db.close()

    try:
        threads = [threading.Thread(target=_initial_balance), threading.Thread(target=_restock)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        kinds = {kind for kind, _ in results}
        assert "initial_balance_ok" in kinds, results
        assert "restock_error" in kinds, results
        restock_error = next(r for k, r in results if k == "restock_error")
        assert restock_error.code == "STALE_VERSION"

        verify_db = real_session_factory()
        try:
            final_ingredient = verify_db.get(Ingredient, ingredient_id)
            # Initial Balance's own values — the Restock never got applied.
            assert final_ingredient.physical_quantity == 100
            assert final_ingredient.weighted_average_unit_cost == 2
            assert final_ingredient.version == 2
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- Purchased Product Inventory: two concurrent Initial-Balance attempts ----------------


def test_two_concurrent_purchased_initial_balances_exactly_one_wins(
    real_session_factory, monkeypatch
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type="PURCHASED")
        setup_db.commit()
        business_id, product_id = business.id, product.id
    finally:
        setup_db.close()

    def attempt(quantity, results):
        db = real_session_factory()
        try:
            business_row = db.get(Business, business_id)
            payload = PurchasedInitialBalanceRequest(quantity=quantity, unit_cost="1")
            purchased_inventory_service.create_initial_balance(
                db, business_row, product_id, payload
            )
            results.append(("ok", quantity))
        except ApiError as exc:
            db.rollback()
            results.append(("api_error", exc))
        finally:
            db.close()

    try:
        results = _run_symmetric_lock_race(
            monkeypatch,
            purchased_inventory_service,
            "get_product_for_business_locked",
            [lambda r: attempt("10", r), lambda r: attempt("20", r)],
        )
        oks = [r for kind, r in results if kind == "ok"]
        errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, results
        assert len(errors) == 1, results
        assert errors[0].code == "PURCHASED_INVENTORY_ALREADY_INITIALIZED"

        verify_db = real_session_factory()
        try:
            row_count = (
                verify_db.query(PurchasedProductInventory)
                .filter(PurchasedProductInventory.product_id == product_id)
                .count()
            )
            assert row_count == 1, "exactly one row, never a duplicate under the UNIQUE constraint"
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- Purchased Product Inventory: two concurrent first-Restock attempts -----------------


def test_two_concurrent_first_restocks_exactly_one_creates_the_row(
    real_session_factory, monkeypatch
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type="PURCHASED")
        setup_db.commit()
        business_id, product_id = business.id, product.id
    finally:
        setup_db.close()

    def attempt(quantity, results):
        db = real_session_factory()
        try:
            business_row = db.get(Business, business_id)
            payload = PurchasedRestockRequest(version=None, quantity=quantity, unit_cost="1")
            purchased_inventory_service.restock_purchased_product(
                db, business_row, product_id, payload
            )
            results.append(("ok", quantity))
        except ApiError as exc:
            db.rollback()
            results.append(("api_error", exc))
        finally:
            db.close()

    try:
        results = _run_symmetric_lock_race(
            monkeypatch,
            purchased_inventory_service,
            "get_product_for_business_locked",
            [lambda r: attempt("10", r), lambda r: attempt("20", r)],
        )
        oks = [r for kind, r in results if kind == "ok"]
        errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, results
        assert len(errors) == 1, results
        assert errors[0].code == "PURCHASED_INVENTORY_STATE_CHANGED"

        verify_db = real_session_factory()
        try:
            row = (
                verify_db.query(PurchasedProductInventory)
                .filter(PurchasedProductInventory.product_id == product_id)
                .one()
            )
            txn_count = (
                verify_db.query(PurchasedProductInventoryTransaction)
                .filter(PurchasedProductInventoryTransaction.product_id == product_id)
                .count()
            )
            assert txn_count == 1, "the loser must never have inserted a second transaction"
            assert row.physical_quantity == int(oks[0])
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)


# --- Purchased Product Inventory: Initial Balance racing a first Restock ----------------


def test_purchased_initial_balance_racing_a_first_restock_exactly_one_creates_the_row(
    real_session_factory, monkeypatch
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type="PURCHASED")
        setup_db.commit()
        business_id, product_id = business.id, product.id
    finally:
        setup_db.close()

    results: list[tuple[str, object]] = []

    def _initial_balance(_results):
        db = real_session_factory()
        try:
            business_row = db.get(Business, business_id)
            payload = PurchasedInitialBalanceRequest(quantity="10", unit_cost="1")
            purchased_inventory_service.create_initial_balance(
                db, business_row, product_id, payload
            )
            _results.append(("initial_balance_ok", None))
        except ApiError as exc:
            db.rollback()
            _results.append(("initial_balance_error", exc))
        finally:
            db.close()

    def _restock(_results):
        db = real_session_factory()
        try:
            business_row = db.get(Business, business_id)
            payload = PurchasedRestockRequest(version=None, quantity="20", unit_cost="1")
            purchased_inventory_service.restock_purchased_product(
                db, business_row, product_id, payload
            )
            _results.append(("restock_ok", None))
        except ApiError as exc:
            db.rollback()
            _results.append(("restock_error", exc))
        finally:
            db.close()

    try:
        results = _run_symmetric_lock_race(
            monkeypatch,
            purchased_inventory_service,
            "get_product_for_business_locked",
            [_initial_balance, _restock],
        )
        oks = [kind for kind, _ in results if kind.endswith("_ok")]
        errors = [(kind, r) for kind, r in results if kind.endswith("_error")]
        assert len(oks) == 1, results
        assert len(errors) == 1, results
        # Which side wins the real Postgres lock is genuinely nondeterministic — the
        # *loser's* rejection code depends on which function's own locked re-check finds
        # the other's just-created row: if Restock lost, its own path raises
        # STATE_CHANGED; if Initial Balance lost, its own path raises
        # ALREADY_INITIALIZED. Either outcome proves the same thing: exactly one row is
        # ever created, and the loser never silently applies itself afterward.
        loser_kind, loser_error = errors[0]
        if loser_kind == "restock_error":
            assert loser_error.code == "PURCHASED_INVENTORY_STATE_CHANGED"
        else:
            assert loser_kind == "initial_balance_error"
            assert loser_error.code == "PURCHASED_INVENTORY_ALREADY_INITIALIZED"

        verify_db = real_session_factory()
        try:
            row_count = (
                verify_db.query(PurchasedProductInventory)
                .filter(PurchasedProductInventory.product_id == product_id)
                .count()
            )
            assert row_count == 1, "never two rows, and the loser never mutated the winner's row"
        finally:
            verify_db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id)
