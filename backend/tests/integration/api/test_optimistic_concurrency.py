"""Optimistic concurrency (Spec §8.33/§17.8; Phase 3 plan v3 §4/§17/§18).

Covers two distinct mechanisms:
1. The application-level pre-check (`check_version`) — a client submits a `version` it
   read earlier in a *separate* request; exercised at the HTTP level in each resource's own
   test file (e.g. `test_customers.py::test_stale_version_on_update_returns_409`,
   `test_products.py`, `test_selling_options.py`).
2. The ORM-level `StaleDataError` backstop (`version_id_col`) for a genuine
   *within-transaction* race, AND the translation of that raw SQLAlchemy exception into
   our own `409 STALE_VERSION` `ApiError` by `commit_or_raise_stale` — exercised here by
   calling the real service-layer functions (`customer_service.update_customer`,
   `product_service.update_product`) from two racing threads, not by manipulating ORM
   objects directly (Checkpoint 3 remediation: the previous version of this test only
   proved bare SQLAlchemy mechanics, never that our own translation code actually runs).

Synchronization: a single start-barrier only guarantees both threads *begin* together, not
that both have read the row before either commits — on a fast local connection one
thread's full read+commit can finish before the other even issues its SELECT, so the
"race" might never overlap. Instead, each thread's `db.commit` is monkeypatched (per
session instance, not the class — production code is untouched) to block on a barrier
immediately before the real commit. Since both threads' internal SELECT (inside
`get_owned_or_404`) is fast and happens well before either reaches that point, waiting on
the barrier there guarantees both threads' in-memory rows still reflect the same
pre-update version when they attempt to commit — making the conflict deterministic.
"""

from __future__ import annotations

import threading

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.ingredient import Ingredient, InventoryTransaction
from app.db.models.product import Product
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.user import User
from app.schemas.customer import CustomerUpdateRequest
from app.schemas.ingredient import IngredientUpdateRequest
from app.schemas.inventory import (
    IngredientInitialBalanceRequest,
    IngredientRestockRequest,
    PurchasedInitialBalanceRequest,
    PurchasedRestockRequest,
)
from app.schemas.product import ProductUpdateRequest
from app.services import (
    customer_service,
    ingredient_inventory_service,
    ingredient_service,
    product_service,
    purchased_inventory_service,
)
from tests.integration.schema.factories import (
    make_business_graph,
    make_customer,
    make_ingredient,
    make_product,
)


def _run_service_version_race(real_session_factory, run_update, row_id, business_id):
    """Runs `run_update(db, business, row_id)` — a call into the *real* service layer —
    in two threads, each on its own real connection/session, with each thread's commit
    forced to wait until both have reached their own commit attempt. Returns the raw
    per-thread outcomes for the caller to assert on."""
    results: list[tuple[str, object]] = []
    commit_barrier = threading.Barrier(2)

    def _attempt() -> None:
        db = real_session_factory()
        original_commit = db.commit

        def _synchronized_commit():
            commit_barrier.wait(timeout=5)
            return original_commit()

        db.commit = _synchronized_commit  # instance-level only; production code untouched
        try:
            business = db.get(Business, business_id)
            run_update(db, business, row_id)
            results.append(("ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("api_error", exc))
        except Exception as exc:  # noqa: BLE001 - capturing for assertion, not swallowing
            db.rollback()
            results.append(("error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


def _cleanup_business_graph(real_session_factory, *, row_model, row_id, business_id, owner_id):
    cleanup = real_session_factory()
    try:
        row = cleanup.get(row_model, row_id)
        if row is not None:
            cleanup.delete(row)
            cleanup.flush()
        biz = cleanup.get(Business, business_id)
        if biz is not None:
            cleanup.delete(biz)
            cleanup.flush()
        user = cleanup.get(User, owner_id)
        if user is not None:
            cleanup.delete(user)
        cleanup.commit()
    finally:
        cleanup.close()


def test_service_layer_concurrent_customer_update_translates_to_stale_version_api_error(
    real_session_factory,
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        customer = make_customer(setup_db, business, name="Race Customer")
        setup_db.commit()
        customer_id = customer.id
        business_id = business.id
        owner_id = business.owner_user_id
    finally:
        setup_db.close()

    def run_update(db, business, row_id) -> None:
        payload = CustomerUpdateRequest(version=1, name="Updated")
        customer_service.update_customer(db, business, row_id, payload)

    try:
        results = _run_service_version_race(
            real_session_factory, run_update, customer_id, business_id
        )
        oks = [r for kind, r in results if kind == "ok"]
        api_errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one ApiError, got {results}"

        # The loser must have gotten *our* translated error, not a raw StaleDataError
        # (which would show up as kind == "error" above, failing the assertion already).
        stale_error = api_errors[0]
        assert isinstance(stale_error, ApiError)
        assert stale_error.status_code == 409
        assert stale_error.code == "STALE_VERSION"
        assert "changed since you opened it" in stale_error.message

        verify_db = real_session_factory()
        try:
            final = verify_db.get(Customer, customer_id)
            assert final.version == 2, "winner's UPDATE should have incremented version by 1"
            assert final.name == "Updated"
        finally:
            verify_db.close()
    finally:
        _cleanup_business_graph(
            real_session_factory,
            row_model=Customer,
            row_id=customer_id,
            business_id=business_id,
            owner_id=owner_id,
        )


def test_service_layer_concurrent_product_update_translates_to_stale_version_api_error(
    real_session_factory,
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, name="Race Product")
        setup_db.commit()
        product_id = product.id
        business_id = business.id
        owner_id = business.owner_user_id
    finally:
        setup_db.close()

    def run_update(db, business, row_id) -> None:
        payload = ProductUpdateRequest(version=1, name="Updated")
        product_service.update_product(db, business, row_id, payload)

    try:
        results = _run_service_version_race(
            real_session_factory, run_update, product_id, business_id
        )
        oks = [r for kind, r in results if kind == "ok"]
        api_errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one ApiError, got {results}"

        stale_error = api_errors[0]
        assert isinstance(stale_error, ApiError)
        assert stale_error.status_code == 409
        assert stale_error.code == "STALE_VERSION"
        assert "changed since you opened it" in stale_error.message

        verify_db = real_session_factory()
        try:
            final = verify_db.get(Product, product_id)
            assert final.version == 2, "winner's UPDATE should have incremented version by 1"
            assert final.name == "Updated"
        finally:
            verify_db.close()
    finally:
        _cleanup_business_graph(
            real_session_factory,
            row_model=Product,
            row_id=product_id,
            business_id=business_id,
            owner_id=owner_id,
        )


def test_service_layer_concurrent_ingredient_update_translates_to_stale_version_api_error(
    real_session_factory,
):
    """Phase 4 Plan v4 §4: Ingredient now carries the same `version_id_col` wiring as
    Product/SellingOption — this proves the real within-transaction race is caught and
    translated identically, not just that the application-level `check_version` pre-check
    works (already covered at the HTTP level in test_ingredients.py)."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business, name="Race Flour")
        setup_db.commit()
        ingredient_id = ingredient.id
        business_id = business.id
        owner_id = business.owner_user_id
    finally:
        setup_db.close()

    def run_update(db, business, row_id) -> None:
        payload = IngredientUpdateRequest(version=1, name="Updated")
        ingredient_service.update_ingredient(db, business, row_id, payload)

    try:
        results = _run_service_version_race(
            real_session_factory, run_update, ingredient_id, business_id
        )
        oks = [r for kind, r in results if kind == "ok"]
        api_errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one ApiError, got {results}"

        stale_error = api_errors[0]
        assert isinstance(stale_error, ApiError)
        assert stale_error.status_code == 409
        assert stale_error.code == "STALE_VERSION"
        assert "changed since you opened it" in stale_error.message

        verify_db = real_session_factory()
        try:
            final = verify_db.get(Ingredient, ingredient_id)
            assert final.version == 2, "winner's UPDATE should have incremented version by 1"
            assert final.name == "Updated"
        finally:
            verify_db.close()
    finally:
        _cleanup_business_graph(
            real_session_factory,
            row_model=Ingredient,
            row_id=ingredient_id,
            business_id=business_id,
            owner_id=owner_id,
        )


def test_service_layer_concurrent_ingredient_restock_translates_to_stale_version_api_error(
    real_session_factory,
):
    """Phase 5 Plan §G: an ordinary Restock against an *already-initialized* Ingredient
    takes no row lock at all — this proves `version_id_col` alone is sufficient to
    prevent a lost update under genuine concurrency, exactly like the plain-CRUD races
    above."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        ingredient = make_ingredient(setup_db, business, name="Race Sugar")
        setup_db.commit()
        ingredient_inventory_service.create_initial_balance(
            setup_db,
            business,
            ingredient.id,
            IngredientInitialBalanceRequest(version=1, quantity="100", unit="g", unit_cost="1"),
        )
        ingredient_id = ingredient.id
        business_id = business.id
        owner_id = business.owner_user_id
        starting_version = ingredient.version
    finally:
        setup_db.close()

    def run_update(db, business, row_id) -> None:
        payload = IngredientRestockRequest(
            version=starting_version, quantity="1", unit="g", unit_cost="5"
        )
        ingredient_inventory_service.restock_ingredient(db, business, row_id, payload)

    try:
        results = _run_service_version_race(
            real_session_factory, run_update, ingredient_id, business_id
        )
        oks = [r for kind, r in results if kind == "ok"]
        api_errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one ApiError, got {results}"

        stale_error = api_errors[0]
        assert isinstance(stale_error, ApiError)
        assert stale_error.status_code == 409
        assert stale_error.code == "STALE_VERSION"

        verify_db = real_session_factory()
        try:
            final = verify_db.get(Ingredient, ingredient_id)
            # Exactly one restock applied — 100 + 1 = 101, never 102 (a lost update
            # would double-apply, and a corrupted read would apply neither correctly).
            assert final.physical_quantity == 101
            assert final.version == starting_version + 1
        finally:
            verify_db.close()
    finally:
        # Delete the InventoryTransaction rows first — `ondelete="NO ACTION"` on
        # `inventory_transactions.ingredient_id` blocks deleting the Ingredient while any
        # reference it (Phase 5's Initial Balance/Restock both leave one behind).
        cleanup = real_session_factory()
        try:
            for txn in cleanup.scalars(
                select(InventoryTransaction).where(
                    InventoryTransaction.ingredient_id == ingredient_id
                )
            ):
                cleanup.delete(txn)
            cleanup.commit()
        finally:
            cleanup.close()
        _cleanup_business_graph(
            real_session_factory,
            row_model=Ingredient,
            row_id=ingredient_id,
            business_id=business_id,
            owner_id=owner_id,
        )


def test_service_layer_concurrent_purchased_inventory_restock_translates_to_stale_version_api_error(
    real_session_factory,
):
    """Phase 5 Plan §G: an ordinary Restock against an *already-existing*
    `PurchasedProductInventory` row takes no Product lock at all (that lock is reserved
    for the row's own creation) — this proves `version_id_col`, freshly wired onto this
    model for Phase 5, alone prevents a lost update under genuine concurrency."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business, product_type="PURCHASED", name="Race Cans")
        setup_db.commit()
        inventory = purchased_inventory_service.create_initial_balance(
            setup_db,
            business,
            product.id,
            PurchasedInitialBalanceRequest(quantity="100", unit_cost="1"),
        )
        product_id = product.id
        business_id = business.id
        owner_id = business.owner_user_id
        starting_version = inventory.version
    finally:
        setup_db.close()

    def run_update(db, business, row_id) -> None:
        payload = PurchasedRestockRequest(version=starting_version, quantity="1", unit_cost="5")
        purchased_inventory_service.restock_purchased_product(db, business, row_id, payload)

    try:
        results = _run_service_version_race(
            real_session_factory, run_update, product_id, business_id
        )
        oks = [r for kind, r in results if kind == "ok"]
        api_errors = [r for kind, r in results if kind == "api_error"]
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(api_errors) == 1, f"expected exactly one ApiError, got {results}"

        stale_error = api_errors[0]
        assert isinstance(stale_error, ApiError)
        assert stale_error.status_code == 409
        assert stale_error.code == "STALE_VERSION"

        verify_db = real_session_factory()
        try:
            final = verify_db.scalar(
                select(PurchasedProductInventory).where(
                    PurchasedProductInventory.product_id == product_id
                )
            )
            assert final.physical_quantity == 101
            assert final.version == starting_version + 1
        finally:
            verify_db.close()
    finally:
        cleanup = real_session_factory()
        try:
            for txn in cleanup.scalars(
                select(PurchasedProductInventoryTransaction).where(
                    PurchasedProductInventoryTransaction.product_id == product_id
                )
            ):
                cleanup.delete(txn)
            row = cleanup.scalar(
                select(PurchasedProductInventory).where(
                    PurchasedProductInventory.product_id == product_id
                )
            )
            if row is not None:
                cleanup.delete(row)
                cleanup.flush()
            cleanup.commit()
        finally:
            cleanup.close()
        _cleanup_business_graph(
            real_session_factory,
            row_model=Product,
            row_id=product_id,
            business_id=business_id,
            owner_id=owner_id,
        )
