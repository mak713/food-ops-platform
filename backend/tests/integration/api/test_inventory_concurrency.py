"""Inventory concurrency/race tests (Phase 5 Plan §G; approved decision 4's required
race coverage): the Initial-Balance/first-Restock creation races for both Ingredient and
Purchased Product Inventory.

The two resources are serialized differently (ADR-110), and the tests below assert exactly
what each mechanism guarantees:

* Ingredient Initial Balance vs. Ingredient Initial Balance — the Ingredient row is locked
  (`SELECT ... FOR UPDATE`) only for the Initial-Balance call, so two concurrent Initial
  Balances serialize on that row lock.
* Ingredient Initial Balance vs. Ingredient Restock — an *asymmetric* race: Restock takes no
  lock, so this is a row-lock plus optimistic-version (`version_id_col`) race in which
  either operation may legitimately commit first. The invariant is not "who wins" but that
  exactly one mutation commits, the loser gets `409 STALE_VERSION`, and no update or history
  row is lost or duplicated.
* Purchased Product Inventory — no inventory row (hence no inventory version) exists before
  the first Initial Balance/Restock, so creation serializes on the shared parent-Product
  lock (ADR-106/113) rather than on any inventory-row version.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from app.core.api_errors import ApiError
from app.db.enums import InventoryTransactionType
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

# Request values shared by the race and the deterministic tests below, chosen so the two
# possible winners leave unmistakably different state:
#   Initial Balance (100 g @ 2): quantity 100, WAC 2, latest-purchase NULL, replacement NULL.
#   Restock (1 g @ 9) on a fresh, never-stocked Ingredient: quantity 1, WAC 9 (a non-positive
#   prior balance does not participate in the blend, ADR-111), latest-purchase 9,
#   replacement NULL.


def _read_ingredient_state(real_session_factory, ingredient_id):
    verify_db = real_session_factory()
    try:
        ingredient = verify_db.get(Ingredient, ingredient_id)
        transactions = (
            verify_db.query(InventoryTransaction)
            .filter(InventoryTransaction.ingredient_id == ingredient_id)
            .all()
        )
        return ingredient, transactions
    finally:
        verify_db.close()


def _assert_initial_balance_state(ingredient, transactions):
    """Initial Balance sets exactly physical quantity and WAC (ADR-110): it never sets
    Latest Purchase Cost and never creates a Replacement Cost override."""
    assert ingredient.physical_quantity == Decimal("100")
    assert ingredient.weighted_average_unit_cost == Decimal("2")
    assert ingredient.latest_purchase_unit_cost is None
    assert ingredient.replacement_unit_cost is None
    assert ingredient.version == 2
    assert len(transactions) == 1, "exactly one history row may exist"
    (txn,) = transactions
    assert txn.transaction_type == InventoryTransactionType.INITIAL_BALANCE
    assert txn.quantity_change == Decimal("100")
    assert txn.unit_cost == Decimal("2")
    assert txn.total_cost == Decimal("200")


def _assert_restock_state(ingredient, transactions):
    """A Restock sets quantity, WAC, and Latest Purchase Cost, and never touches the
    Replacement Cost override (approval decision 5)."""
    assert ingredient.physical_quantity == Decimal("1")
    assert ingredient.weighted_average_unit_cost == Decimal("9")
    assert ingredient.latest_purchase_unit_cost == Decimal("9")
    assert ingredient.replacement_unit_cost is None
    assert ingredient.version == 2
    assert len(transactions) == 1, "exactly one history row may exist"
    (txn,) = transactions
    assert txn.transaction_type == InventoryTransactionType.RESTOCK
    assert txn.quantity_change == Decimal("1")
    assert txn.unit_cost == Decimal("9")
    assert txn.total_cost == Decimal("9")


def _new_unstocked_ingredient(real_session_factory):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()
        return business.id, ingredient.id
    finally:
        setup_db.close()


def _initial_balance_call(real_session_factory, business_id, ingredient_id):
    db = real_session_factory()
    try:
        business_row = db.get(Business, business_id)
        payload = IngredientInitialBalanceRequest(
            version=1, quantity="100", unit="g", unit_cost="2"
        )
        ingredient_inventory_service.create_initial_balance(
            db, business_row, ingredient_id, payload
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _restock_call(real_session_factory, business_id, ingredient_id):
    db = real_session_factory()
    try:
        business_row = db.get(Business, business_id)
        payload = IngredientRestockRequest(version=1, quantity="1", unit="g", unit_cost="9")
        ingredient_inventory_service.restock_ingredient(db, business_row, ingredient_id, payload)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_ingredient_initial_balance_racing_a_concurrent_restock_never_loses_an_update(
    real_session_factory,
):
    """Initial Balance (which locks the Ingredient row) racing Restock (which takes no
    lock) is an *asymmetric* race: the outcome combines the row lock with the optimistic
    `version_id_col` check, and EITHER operation may legitimately commit first depending
    on scheduling. This test deliberately does not assume a winner (a CI run saw Restock
    win; a local run typically sees Initial Balance win; both are correct).

    Whichever commits first bumps `version` 1 -> 2 atomically with its own history row, so
    the other operation, which submitted `version=1`, can only end in `409 STALE_VERSION`
    (synchronously at `check_version` if it reads after the commit, or at
    `commit_or_raise_stale` if its `UPDATE` raced the winner). The invariant verified is:
    both threads terminate; exactly one operation succeeds and exactly one gets a
    structured `STALE_VERSION` `ApiError` (no raw exception escapes); exactly one history
    row exists; the final state is precisely the winner's own values at `version == 2`;
    nothing from the loser survives."""
    business_id, ingredient_id = _new_unstocked_ingredient(real_session_factory)

    start_barrier = threading.Barrier(2)
    results: list[tuple[str, str, object]] = []  # (operation, outcome, error-or-None)

    def _run(operation, call):
        try:
            start_barrier.wait(timeout=5)
            call(real_session_factory, business_id, ingredient_id)
            results.append((operation, "ok", None))
        except ApiError as exc:
            results.append((operation, "api_error", exc))
        except Exception as exc:  # noqa: BLE001 - recorded so the assertions below fail loudly
            results.append((operation, "raw_exception", exc))

    try:
        threads = [
            threading.Thread(target=_run, args=("initial_balance", _initial_balance_call)),
            threading.Thread(target=_run, args=("restock", _restock_call)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not any(t.is_alive() for t in threads), "a thread never terminated"
        assert len(results) == 2, results
        raw = [r for r in results if r[1] == "raw_exception"]
        assert not raw, f"a raw (non-ApiError) exception escaped: {raw}"

        winners = [r for r in results if r[1] == "ok"]
        losers = [r for r in results if r[1] == "api_error"]
        assert len(winners) == 1, results
        assert len(losers) == 1, results
        winner_operation = winners[0][0]
        loser_operation, _, loser_error = losers[0]
        assert loser_operation != winner_operation
        assert isinstance(loser_error, ApiError)
        assert loser_error.status_code == 409
        assert loser_error.code == "STALE_VERSION", loser_error.code

        ingredient, transactions = _read_ingredient_state(real_session_factory, ingredient_id)
        if winner_operation == "initial_balance":
            _assert_initial_balance_state(ingredient, transactions)  # Restock left no trace
        else:
            assert winner_operation == "restock", winner_operation
            _assert_restock_state(ingredient, transactions)  # Initial Balance left no trace
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_restock_committing_first_rejects_a_stale_initial_balance_without_overwriting_it(
    real_session_factory,
):
    """Deterministic complement to the race test above for the branch a scheduler may or
    may not produce: Restock commits first, so a later Initial Balance that still carries
    the pre-Restock `version=1` is rejected with `STALE_VERSION` and leaves the Restock's
    state and history row exactly intact."""
    business_id, ingredient_id = _new_unstocked_ingredient(real_session_factory)
    try:
        _restock_call(real_session_factory, business_id, ingredient_id)
        with pytest.raises(ApiError) as exc_info:
            _initial_balance_call(real_session_factory, business_id, ingredient_id)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "STALE_VERSION"

        ingredient, transactions = _read_ingredient_state(real_session_factory, ingredient_id)
        _assert_restock_state(ingredient, transactions)
    finally:
        _cleanup(real_session_factory, business_id=business_id)


def test_initial_balance_committing_first_rejects_a_stale_restock_without_applying_it(
    real_session_factory,
):
    """Deterministic complement for the opposite branch: Initial Balance commits first, so
    a later Restock still carrying `version=1` is rejected with `STALE_VERSION` and no
    Restock mutation or history row survives."""
    business_id, ingredient_id = _new_unstocked_ingredient(real_session_factory)
    try:
        _initial_balance_call(real_session_factory, business_id, ingredient_id)
        with pytest.raises(ApiError) as exc_info:
            _restock_call(real_session_factory, business_id, ingredient_id)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "STALE_VERSION"

        ingredient, transactions = _read_ingredient_state(real_session_factory, ingredient_id)
        _assert_initial_balance_state(ingredient, transactions)
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
