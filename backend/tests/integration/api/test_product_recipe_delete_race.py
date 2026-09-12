"""Concurrent first-Recipe creation vs. Product deletion — Phase 4 Plan v4 §6c.

Proves the approved cross-phase concurrency hardening in
`product_service.get_product_for_business_locked` (shared by `delete_product` and Phase
4's `recipe_service.create_recipe`) actually closes the race: without it, Product deletion
could pre-check "no Recipe exists," a concurrent request could create that Product's first
Recipe, and the original deletion could still proceed, CASCADE-deleting the just-created
Recipe. With it, the two workflows serialize on the Product row and only two outcomes are
possible — a lost Recipe is not one of them.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.product import Product
from app.db.models.recipe import Recipe
from app.db.models.user import User
from app.schemas.recipe import RecipeCreateRequest, RecipeRevisionIngredientCreateRequest
from app.services import product_service, recipe_service
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import make_business_graph, make_ingredient, make_product


def _run_delete_vs_create_recipe_race(
    real_session_factory, business_id, product_id, product_version, ingredient_id, monkeypatch
):
    """Deterministically forces the dangerous interleaving, rather than merely hoping
    `threading.Barrier(2)`-synchronized start times happen to produce it (remediation
    item 5). Both `delete_product` and `create_recipe` obtain the Product row lock via
    the exact same function, `product_service.get_product_for_business_locked` — but
    each is called through a different name binding (`product_service.py` calls it as a
    bare module-level name; `recipe_service.py` imports its own reference at import
    time), so both bindings are wrapped separately below.

    The wrapper lets the real `SELECT ... FOR UPDATE` run for real (genuine row locking,
    genuine separate connections via `real_session_factory` — no mocked DB behavior).
    Whichever of the two threads' underlying call returns *first* (i.e. actually won the
    real Postgres lock) is provably the "winner": its wrapper claims that status under a
    plain `threading.Lock` and then blocks on `release_gate` — deliberately holding its
    transaction (and therefore the real lock) open. The main test thread waits until (a)
    a winner has been confirmed and (b) *both* threads' wrappers have been entered (i.e.
    both genuinely attempted to acquire the same lock) before setting `release_gate` —
    so by construction, the loser's own attempt was registered, and provably still
    contending for the same row, while the winner actively held it. Only then does the
    winner proceed, releasing the lock and letting the loser's blocked call finally
    return. No sleep-based timing is used anywhere in this coordination."""
    results: list[tuple[str, object]] = []

    about_to_lock_events = {"delete": threading.Event(), "create": threading.Event()}
    winner_confirmed_event = threading.Event()
    release_gate = threading.Event()
    winner_holder: dict[str, str | None] = {"label": None}
    winner_lock = threading.Lock()

    real_locked_fn = product_service.get_product_for_business_locked

    def make_wrapper(label: str):
        def wrapper(db, product_id_, business_):
            about_to_lock_events[label].set()
            result = real_locked_fn(db, product_id_, business_)
            with winner_lock:
                if winner_holder["label"] is None:
                    winner_holder["label"] = label
                    winner_confirmed_event.set()
                    should_wait = True
                else:
                    should_wait = False
            if should_wait:
                # Hold the real lock open until the test confirms the other side's
                # attempt is genuinely in flight against this same row.
                release_gate.wait(timeout=5)
            return result

        return wrapper

    # `product_service.delete_product` resolves `get_product_for_business_locked` as a
    # bare name in its own module's globals; `recipe_service.create_recipe` resolves its
    # own separately-imported name in *its* module's globals. Both must be patched.
    monkeypatch.setattr(product_service, "get_product_for_business_locked", make_wrapper("delete"))
    monkeypatch.setattr(recipe_service, "get_product_for_business_locked", make_wrapper("create"))

    start_barrier = threading.Barrier(2)

    def _delete_product() -> None:
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business = db.get(Business, business_id)
            product_service.delete_product(db, business, product_id, product_version)
            results.append(("delete_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("delete_api_error", exc))
        finally:
            db.close()

    def _create_recipe() -> None:
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business = db.get(Business, business_id)
            payload = RecipeCreateRequest(
                name="Race Recipe",
                yield_quantity=Decimal("10"),
                active_time_minutes=10,
                ingredients=[
                    RecipeRevisionIngredientCreateRequest(
                        ingredient_id=str(ingredient_id), quantity=Decimal("1"), unit="g"
                    )
                ],
            )
            recipe_service.create_recipe(db, business, product_id, payload)
            results.append(("create_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("create_api_error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_delete_product), threading.Thread(target=_create_recipe)]
    for t in threads:
        t.start()

    # Proof point: a winner has actually acquired the real DB lock AND both sides'
    # attempts are registered as having reached the lock call — genuine overlap, not an
    # artifact of start-time-only synchronization.
    assert winner_confirmed_event.wait(timeout=5), "no thread ever acquired the Product lock"
    assert about_to_lock_events["delete"].wait(timeout=5), "delete_product never attempted the lock"
    assert about_to_lock_events["create"].wait(timeout=5), "create_recipe never attempted the lock"

    release_gate.set()
    for t in threads:
        t.join(timeout=10)
    return results


def test_concurrent_product_deletion_and_first_recipe_creation_cannot_lose_the_recipe(
    real_session_factory, monkeypatch
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()
        business_id, product_id = business.id, product.id
        product_version = product.version
        ingredient_id = ingredient.id
    finally:
        setup_db.close()

    results = _run_delete_vs_create_recipe_race(
        real_session_factory, business_id, product_id, product_version, ingredient_id, monkeypatch
    )
    kinds = {kind for kind, _ in results}
    assert len(results) == 2, results

    verify_db = real_session_factory()
    try:
        product_row = verify_db.get(Product, product_id)
        recipe_row = verify_db.scalar(select(Recipe).where(Recipe.product_id == product_id))

        if "delete_ok" in kinds:
            # Deletion won: the product (and therefore any recipe) is fully gone, and
            # recipe creation must have been rejected as "product not found" — never
            # silently orphaned/created against a since-deleted product.
            assert product_row is None
            assert recipe_row is None
            create_error = next(r for k, r in results if k == "create_api_error")
            assert create_error.status_code == 404
        else:
            # Recipe creation won: the product is untouched and its just-created Recipe
            # must still exist — never silently CASCADE-deleted — and deletion must have
            # been rejected specifically as PRODUCT_HAS_RECIPE.
            assert "create_ok" in kinds
            assert product_row is not None
            assert recipe_row is not None
            delete_error = next(r for k, r in results if k == "delete_api_error")
            assert delete_error.status_code == 409
            assert delete_error.code == "PRODUCT_HAS_RECIPE"
    finally:
        verify_db.close()
        _cleanup(real_session_factory, product_id, ingredient_id, business_id)


# --- Rollback-on-expected-failure proofs (remediation item 4) ----------------------


def test_delete_product_stale_version_failure_releases_the_product_lock(client, session):
    """Regression: `delete_product` acquires the Product row's `FOR UPDATE` lock via
    `get_product_for_business_locked` before its version check. When that check raises
    the expected `STALE_VERSION` `ApiError`, the lock-owning transaction must be
    explicitly rolled back before the error propagates — not left open."""
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Sourdough Loaf", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    response = client.delete(
        f"/api/v1/products/{product['id']}?version={product['version'] + 1}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STALE_VERSION"

    assert session.in_transaction() is False
    assert session.scalar(select(Product).where(Product.id == product["id"])) is not None


def test_delete_product_has_recipe_failure_releases_the_product_lock(client, session):
    """Regression: same proof as above, for the other expected-rejection path inside the
    locked section of `delete_product` — the Recipe-existence pre-check."""
    signup(client)
    product = client.post(
        "/api/v1/products",
        json={"name": "Sourdough Loaf", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()
    ingredient = client.post(
        "/api/v1/ingredients",
        json={"name": "Flour", "measurement_family": "WEIGHT", "canonical_unit": "g"},
        headers=csrf_headers(client),
    ).json()
    client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json={
            "name": "House Sourdough",
            "yield_quantity": "12",
            "active_time_minutes": 30,
            "ingredients": [{"ingredient_id": ingredient["id"], "quantity": "500", "unit": "g"}],
        },
        headers=csrf_headers(client),
    )

    response = client.delete(
        f"/api/v1/products/{product['id']}?version={product['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PRODUCT_HAS_RECIPE"

    assert session.in_transaction() is False
    assert session.scalar(select(Product).where(Product.id == product["id"])) is not None


def _cleanup(real_session_factory, product_id, ingredient_id, business_id):
    cleanup = real_session_factory()
    try:
        recipe_row = cleanup.scalar(select(Recipe).where(Recipe.product_id == product_id))
        if recipe_row is not None:
            cleanup.delete(recipe_row)
            cleanup.flush()
        ingredient_row = cleanup.get(Ingredient, ingredient_id)
        if ingredient_row is not None:
            cleanup.delete(ingredient_row)
            cleanup.flush()
        product_row = cleanup.get(Product, product_id)
        if product_row is not None:
            cleanup.delete(product_row)
            cleanup.flush()
        business_row = cleanup.get(Business, business_id)
        if business_row is not None:
            owner_id = business_row.owner_user_id
            cleanup.delete(business_row)
            cleanup.flush()
            user_row = cleanup.get(User, owner_id)
            if user_row is not None:
                cleanup.delete(user_row)
        cleanup.commit()
    finally:
        cleanup.close()
