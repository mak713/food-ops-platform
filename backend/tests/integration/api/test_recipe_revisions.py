"""RecipeRevision creation/immutability/concurrency/pagination — Phase 4 Plan v4 §6/§7/§14."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.recipe import Recipe, RecipeRevision
from app.schemas.recipe import (
    RecipeCreateRequest,
    RecipeRevisionCreateRequest,
    RecipeRevisionIngredientCreateRequest,
)
from app.services import ingredient_service, recipe_service
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import make_business_graph, make_ingredient, make_product


def _create_product(client, **overrides):
    signup(client)
    payload = {"name": "Sourdough Loaf", "product_type": "PRODUCED"}
    payload.update(overrides)
    return client.post("/api/v1/products", json=payload, headers=csrf_headers(client)).json()


def _create_ingredient(client, **overrides):
    payload = {"name": "Flour", "measurement_family": "WEIGHT", "canonical_unit": "g"}
    payload.update(overrides)
    return client.post("/api/v1/ingredients", json=payload, headers=csrf_headers(client)).json()


def _one_line(ingredient_id, **overrides):
    line = {"ingredient_id": ingredient_id, "quantity": "500", "unit": "g"}
    line.update(overrides)
    return line


def _recipe_payload(ingredient_id, **overrides):
    payload = {
        "name": "House Sourdough",
        "yield_quantity": "12",
        "active_time_minutes": 30,
        "ingredients": [_one_line(ingredient_id)],
    }
    payload.update(overrides)
    return payload


def _revision_payload(expected_current_revision_id, ingredient_id, **overrides):
    payload = {
        "expected_current_revision_id": expected_current_revision_id,
        "yield_quantity": "24",
        "active_time_minutes": 45,
        "ingredients": [_one_line(ingredient_id, quantity="1000")],
    }
    payload.update(overrides)
    return payload


def _setup_recipe(client):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    recipe = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"]),
        headers=csrf_headers(client),
    ).json()
    return product, ingredient, recipe


# --- Basic creation/immutability/listing --------------------------------------------


def test_create_new_revision_succeeds_and_becomes_current(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revision_number"] == 2
    assert body["is_current"] is True
    assert body["yield_quantity"] == "24.000000"

    fetched = client.get(f"/api/v1/products/{product['id']}/recipe").json()
    assert fetched["current_revision"]["id"] == body["id"]


def test_revision_numbers_increment_sequentially(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    rev2 = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, ingredient["id"]),
        headers=csrf_headers(client),
    ).json()
    assert rev2["revision_number"] == 2

    rev3 = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(rev2["id"], ingredient["id"], yield_quantity="36"),
        headers=csrf_headers(client),
    ).json()
    assert rev3["revision_number"] == 3
    assert rev3["yield_quantity"] == "36.000000"


def test_prior_revision_content_is_unchanged_after_new_revision_created(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1 = recipe["current_revision"]

    client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1["id"], ingredient["id"]),
        headers=csrf_headers(client),
    )

    refetched = client.get(
        f"/api/v1/products/{product['id']}/recipe/revisions/{revision1['id']}"
    ).json()
    assert refetched["is_current"] is False
    assert refetched["yield_quantity"] == revision1["yield_quantity"]
    assert refetched["active_time_minutes"] == revision1["active_time_minutes"]
    assert refetched["ingredients"] == revision1["ingredients"]


def test_stale_expected_current_revision_returns_conflict(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    # A second editor's save, based on revision 1, succeeds and becomes revision 2.
    client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, ingredient["id"]),
        headers=csrf_headers(client),
    )

    # A third save, still carrying the now-stale revision 1 as its base, must be rejected.
    stale = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, ingredient["id"], yield_quantity="99"),
        headers=csrf_headers(client),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RECIPE_REVISION_CONFLICT"

    revisions = client.get(f"/api/v1/products/{product['id']}/recipe/revisions").json()
    assert revisions["total"] == 2  # the stale attempt created nothing


def test_create_revision_rejects_malformed_expected_current_revision_id_with_422_not_500(
    client,
):
    """Regression: `expected_current_revision_id` is now a `uuid.UUID`-typed Pydantic
    field (not `str`), so a malformed value is rejected at request validation — never
    reaches service code that would previously compare it as a raw string and could
    otherwise be misused unsafely."""
    product, ingredient, recipe = _setup_recipe(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload("not-a-valid-uuid", ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text


def test_create_revision_accepts_equivalent_uuid_text_representation_without_false_conflict(
    client,
):
    """Regression: an `expected_current_revision_id` submitted in a different but
    equivalent textual UUID representation (differing only in case) must be normalized by
    Pydantic and compared correctly against the actual current revision — it must NOT
    produce a false `RECIPE_REVISION_CONFLICT` merely because the two strings differ
    textually."""
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]
    uppercase_revision1_id = revision1_id.upper()
    assert uppercase_revision1_id != revision1_id  # sanity: genuinely different text

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(uppercase_revision1_id, ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    assert response.json()["revision_number"] == 2


def test_create_revision_rejects_duplicate_ingredient_lines_using_different_uuid_representations(
    client,
):
    """Regression: same duplicate-detection proof as Recipe creation
    (test_recipes.py), for the replacement-revision path."""
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]
    lowercase_id = ingredient["id"]
    uppercase_id = ingredient["id"].upper()
    assert lowercase_id != uppercase_id  # sanity: genuinely different text

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(
            revision1_id,
            lowercase_id,
            ingredients=[
                _one_line(lowercase_id),
                _one_line(uppercase_id, quantity="10"),
            ],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text


def test_create_revision_ingredient_validation_failure_releases_the_recipe_lock(client, session):
    """Regression (remediation item 4): `create_recipe_revision` holds the Recipe row's
    `FOR UPDATE` lock via `lock_recipe_for_product`, then calls
    `_validate_and_lock_ingredient_lines`, which also locks the submitted Ingredient
    rows. When that call raises its expected `RECIPE_REVISION_INVALID_INGREDIENTS`
    `ApiError`, the transaction holding both locks must be explicitly rolled back before
    the error propagates — not left open. Proven the same way as the equivalent
    `create_recipe` proof in test_recipes.py."""
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]
    other_ingredient = _create_ingredient(client, name="Salt")
    client.post(
        f"/api/v1/ingredients/{other_ingredient['id']}/archive",
        json={"version": other_ingredient["version"]},
        headers=csrf_headers(client),
    )

    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, other_ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "RECIPE_REVISION_INVALID_INGREDIENTS"

    # The lock-owning transaction must have been rolled back, not left open.
    assert session.in_transaction() is False
    # And the session itself must remain fully usable — a fresh query succeeds cleanly.
    assert session.scalar(select(Recipe).where(Recipe.id == recipe["id"])) is not None


def test_create_revision_requires_at_least_one_ingredient_line(client):
    product, ingredient, recipe = _setup_recipe(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(recipe["current_revision"]["id"], ingredient["id"], ingredients=[]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_create_revision_rejects_cross_family_unit(client):
    product, ingredient, recipe = _setup_recipe(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(
            recipe["current_revision"]["id"],
            ingredient["id"],
            ingredients=[_one_line(ingredient["id"], unit="mL")],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "CROSS_FAMILY_UNIT_CONVERSION"


def test_create_revision_rejects_duplicate_ingredient_lines(client):
    product, ingredient, recipe = _setup_recipe(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(
            recipe["current_revision"]["id"],
            ingredient["id"],
            ingredients=[_one_line(ingredient["id"]), _one_line(ingredient["id"], quantity="1")],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_create_revision_rejects_missing_ingredient_id(client):
    product, ingredient, recipe = _setup_recipe(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(
            recipe["current_revision"]["id"],
            ingredient["id"],
            ingredients=[_one_line("00000000-0000-0000-0000-000000000000")],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "INGREDIENT_NOT_FOUND"


# --- Archived-ingredient carry-forward rule ------------------------------------------


def test_archived_ingredient_carried_forward_from_base_revision_is_allowed(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    client.post(
        f"/api/v1/ingredients/{ingredient['id']}/archive",
        json={"version": ingredient["version"]},
        headers=csrf_headers(client),
    )

    # Same ingredient, already present in the base (revision 1) — allowed even archived.
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, ingredient["id"]),
        headers=csrf_headers(client),
    )
    assert response.status_code == 201, response.text
    assert response.json()["ingredients"][0]["ingredient_is_active"] is False


def test_archived_ingredient_not_in_base_revision_is_rejected(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    other_ingredient = _create_ingredient(client, name="Sugar")
    client.post(
        f"/api/v1/ingredients/{other_ingredient['id']}/archive",
        json={"version": other_ingredient["version"]},
        headers=csrf_headers(client),
    )

    # `other_ingredient` was never in revision 1 — introducing it archived is rejected.
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(
            revision1_id,
            ingredient["id"],
            ingredients=[
                _one_line(ingredient["id"]),
                _one_line(other_ingredient["id"], quantity="10"),
            ],
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["issues"][0]["code"] == "INGREDIENT_NOT_ACTIVE"


# --- History pagination + detail ------------------------------------------------------


def test_list_recipe_revisions_is_paginated_and_ordered_descending(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]
    rev2 = client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(revision1_id, ingredient["id"]),
        headers=csrf_headers(client),
    ).json()
    client.post(
        f"/api/v1/products/{product['id']}/recipe/revisions",
        json=_revision_payload(rev2["id"], ingredient["id"]),
        headers=csrf_headers(client),
    )

    full = client.get(f"/api/v1/products/{product['id']}/recipe/revisions")
    assert full.status_code == 200
    body = full.json()
    assert body["total"] == 3
    assert [r["revision_number"] for r in body["items"]] == [3, 2, 1]

    paged = client.get(f"/api/v1/products/{product['id']}/recipe/revisions?limit=1&offset=1")
    assert paged.json()["total"] == 3
    assert len(paged.json()["items"]) == 1
    assert paged.json()["items"][0]["revision_number"] == 2


def test_get_recipe_revision_detail(client):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    response = client.get(f"/api/v1/products/{product['id']}/recipe/revisions/{revision1_id}")
    assert response.status_code == 200
    assert response.json()["id"] == revision1_id


def test_get_recipe_revision_detail_wrong_recipe_returns_404(client):
    product_a, ingredient_a, recipe_a = _setup_recipe(client)
    product_b = client.post(
        "/api/v1/products",
        json={"name": "Other Product", "product_type": "PRODUCED"},
        headers=csrf_headers(client),
    ).json()

    response = client.get(
        f"/api/v1/products/{product_b['id']}/recipe/revisions/{recipe_a['current_revision']['id']}"
    )
    # product_b has no recipe at all yet, so this 404s before even reaching the revision lookup.
    assert response.status_code == 404


# --- NUMERIC(18,6) precision boundaries (Phase 4 Plan v4 §11) -----------------------


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [
        ("999999999999.999999", 201),  # exactly 18 total digits, at the limit
        ("1000000000000.000000", 422),  # 19 digit-tuple digits, over the limit
        ("0.000001", 201),  # exactly 6 fractional places
        ("0.0000001", 422),  # 7 fractional places
        ("5.000000", 201),  # harmless trailing zeros must not be over-rejected
    ],
)
def test_yield_quantity_numeric_18_6_boundaries(client, value, expected_status):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(ingredient["id"], yield_quantity=value),
        headers=csrf_headers(client),
    )
    assert response.status_code == expected_status, response.text


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [
        ("999999999999.999999", 201),
        ("1000000000000.000000", 422),
        ("0.000001", 201),
        ("0.0000001", 422),
        ("5.000000", 201),
    ],
)
def test_recipe_revision_ingredient_quantity_numeric_18_6_boundaries(
    client, value, expected_status
):
    product = _create_product(client)
    ingredient = _create_ingredient(client)
    response = client.post(
        f"/api/v1/products/{product['id']}/recipe",
        json=_recipe_payload(
            ingredient["id"], ingredients=[_one_line(ingredient["id"], quantity=value)]
        ),
        headers=csrf_headers(client),
    )
    assert response.status_code == expected_status, response.text


# --- Broken current-revision invariant (Phase 4 Plan v4 §6a Correction 2) -----------


def test_broken_zero_current_revision_invariant_is_not_silently_repaired(client, session):
    product, ingredient, recipe = _setup_recipe(client)
    revision1_id = recipe["current_revision"]["id"]

    # Corrupt state directly, bypassing the service layer entirely — simulates a
    # should-be-impossible condition the service must refuse to silently paper over.
    session.execute(
        update(RecipeRevision).where(RecipeRevision.id == revision1_id).values(is_current=False)
    )
    session.commit()

    business = session.execute(select(Business)).scalar_one()
    payload = RecipeRevisionCreateRequest(
        expected_current_revision_id=revision1_id,
        yield_quantity=Decimal("10"),
        active_time_minutes=10,
        ingredients=[
            RecipeRevisionIngredientCreateRequest(
                ingredient_id=ingredient["id"], quantity=Decimal("1"), unit="g"
            )
        ],
    )
    import uuid

    with pytest.raises(RuntimeError):
        recipe_service.create_recipe_revision(session, business, uuid.UUID(product["id"]), payload)


# --- Real concurrency: stale editor rejected (Phase 4 Plan v4 §6a manual scenario) --


def _run_revision_race(
    real_session_factory, business_id, product_id, expected_revision_id, ingredient_id
):
    results: list[tuple[str, object]] = []
    start_barrier = threading.Barrier(2)

    def _attempt() -> None:
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business = db.get(Business, business_id)
            payload = RecipeRevisionCreateRequest(
                expected_current_revision_id=str(expected_revision_id),
                yield_quantity=Decimal("50"),
                active_time_minutes=20,
                ingredients=[
                    RecipeRevisionIngredientCreateRequest(
                        ingredient_id=str(ingredient_id), quantity=Decimal("100"), unit="g"
                    )
                ],
            )
            recipe_service.create_recipe_revision(db, business, product_id, payload)
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


def test_concurrent_replacement_revisions_based_on_the_same_revision_only_one_wins(
    real_session_factory,
):
    """The exact two-tab manual scenario (Phase 4 Plan v4 §6a/§15), proven with real
    PostgreSQL sessions: both threads open their editor against revision 1; only one may
    win and become revision 2 — the other must get RECIPE_REVISION_CONFLICT, never a
    silent second "success" based on stale content."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()

        recipe, revision1 = recipe_service.create_recipe(
            setup_db,
            business,
            product.id,
            RecipeCreateRequest(
                name="Race Recipe",
                yield_quantity=Decimal("10"),
                active_time_minutes=10,
                ingredients=[
                    RecipeRevisionIngredientCreateRequest(
                        ingredient_id=str(ingredient.id), quantity=Decimal("1"), unit="g"
                    )
                ],
            ),
        )
        business_id = business.id
        product_id = product.id
        revision1_id = revision1.id
        ingredient_id = ingredient.id
        recipe_id = recipe.id
    finally:
        setup_db.close()

    results = _run_revision_race(
        real_session_factory, business_id, product_id, revision1_id, ingredient_id
    )
    oks = [r for kind, r in results if kind == "ok"]
    api_errors = [r for kind, r in results if kind == "api_error"]
    assert len(oks) == 1, f"expected exactly one winner, got {results}"
    assert len(api_errors) == 1, f"expected exactly one conflict, got {results}"
    assert api_errors[0].status_code == 409
    assert api_errors[0].code == "RECIPE_REVISION_CONFLICT"

    verify_db = real_session_factory()
    try:
        current_revisions = list(
            verify_db.scalars(
                select(RecipeRevision).where(
                    RecipeRevision.recipe_id == recipe_id,
                    RecipeRevision.is_current == True,  # noqa: E712
                )
            )
        )
        assert len(current_revisions) == 1
        assert current_revisions[0].revision_number == 2
    finally:
        verify_db.close()
        _cleanup_recipe_graph(real_session_factory, product_id, ingredient_id, business_id)


# --- Real concurrency: Ingredient archive/delete vs. Recipe composition validation --


def _run_recipe_creation_vs_ingredient_mutation_race(
    real_session_factory, business_id, product_id, ingredient_id, ingredient_version, mutate
):
    results: list[tuple[str, object]] = []
    start_barrier = threading.Barrier(2)

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
            results.append(("recipe_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("recipe_api_error", exc))
        finally:
            db.close()

    def _mutate_ingredient() -> None:
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business = db.get(Business, business_id)
            mutate(db, business, ingredient_id, ingredient_version)
            results.append(("mutate_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("mutate_api_error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=_create_recipe), threading.Thread(target=_mutate_ingredient)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


def _run_recipe_creation_vs_ingredient_archive_race_deterministic(
    real_session_factory, business_id, product_id, ingredient_id, ingredient_version, monkeypatch
):
    """Deterministic strengthening of the Barrier-only race above (remediation item 5),
    for the archive side specifically. `threading.Barrier(2)` alone only synchronizes
    *start* times — it cannot guarantee the recipe-creation transaction has actually
    acquired the Ingredient row lock (via `ingredient_service.lock_ingredients_for_business`,
    called as `ingredient_service.lock_ingredients_for_business(...)` inside
    `recipe_service._validate_and_lock_ingredient_lines` — a module-attribute access, so
    patching the `ingredient_service` module attribute correctly intercepts that call
    site) at the moment the archive side attempts its own conflicting `UPDATE`.

    Here, the archive side's actual mutating commit (`ingredient_service.commit_or_raise_stale`,
    also a module-attribute call site from `archive_ingredient`) is deliberately gated to
    wait until the lock-acquisition wrapper confirms the recipe-creation side has
    genuinely acquired the real Postgres row lock — so the archive attempt is proven to
    occur while that lock is actively held, then the lock-holder is released only once
    the archive side's attempt is confirmed in flight. This forces (rather than merely
    hopes for) the specific dangerous interleaving the locking mechanism exists to
    protect against, with no `sleep()`-based timing anywhere."""
    results: list[tuple[str, object]] = []

    lock_acquired_event = threading.Event()
    about_to_commit_event = threading.Event()
    release_gate = threading.Event()

    real_lock_fn = ingredient_service.lock_ingredients_for_business

    def wrapped_lock(db, ingredient_ids, business):
        result = real_lock_fn(db, ingredient_ids, business)
        lock_acquired_event.set()
        release_gate.wait(timeout=5)
        return result

    real_commit_or_raise_stale = ingredient_service.commit_or_raise_stale

    def wrapped_commit_or_raise_stale(db, *, resource):
        about_to_commit_event.set()
        # Prove the archive's actual UPDATE/commit is attempted only once the
        # recipe-creation side genuinely holds the row lock — not merely "around the
        # same time" as it.
        assert lock_acquired_event.wait(timeout=5), "recipe creation never acquired the lock"
        return real_commit_or_raise_stale(db, resource=resource)

    monkeypatch.setattr(ingredient_service, "lock_ingredients_for_business", wrapped_lock)
    monkeypatch.setattr(ingredient_service, "commit_or_raise_stale", wrapped_commit_or_raise_stale)

    start_barrier = threading.Barrier(2)

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
            results.append(("recipe_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("recipe_api_error", exc))
        finally:
            db.close()

    def _archive_ingredient() -> None:
        db = real_session_factory()
        start_barrier.wait(timeout=5)
        try:
            business = db.get(Business, business_id)
            ingredient_service.archive_ingredient(db, business, ingredient_id, ingredient_version)
            results.append(("mutate_ok", None))
        except ApiError as exc:
            db.rollback()
            results.append(("mutate_api_error", exc))
        finally:
            db.close()

    threads = [
        threading.Thread(target=_create_recipe),
        threading.Thread(target=_archive_ingredient),
    ]
    for t in threads:
        t.start()

    assert lock_acquired_event.wait(timeout=5), "recipe creation never acquired the Ingredient lock"
    assert about_to_commit_event.wait(timeout=5), "archive never attempted its commit"

    release_gate.set()
    for t in threads:
        t.join(timeout=10)
    return results


def test_ingredient_archive_races_recipe_creation_without_producing_an_invalid_state(
    real_session_factory, monkeypatch
):
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()
        business_id, product_id = business.id, product.id
        ingredient_id, ingredient_version = ingredient.id, ingredient.version
    finally:
        setup_db.close()

    results = _run_recipe_creation_vs_ingredient_archive_race_deterministic(
        real_session_factory,
        business_id,
        product_id,
        ingredient_id,
        ingredient_version,
        monkeypatch,
    )
    kinds = {kind for kind, _ in results}
    assert "mutate_ok" in kinds, f"archive should always succeed: {results}"
    recipe_outcome = [r for kind, r in results if kind.startswith("recipe_")]
    assert len(recipe_outcome) == 1

    verify_db = real_session_factory()
    try:
        final_ingredient = verify_db.get(Ingredient, ingredient_id)
        assert final_ingredient.is_active is False  # archive always wins eventually
        recipe_row = verify_db.scalar(select(Recipe).where(Recipe.product_id == product_id))
        if recipe_row is not None:
            # Recipe creation committed first (saw the ingredient while still active).
            assert any(kind == "recipe_ok" for kind, _ in results)
        else:
            # Ingredient was archived first; recipe creation correctly rejected it.
            recipe_kind, recipe_error = next((k, r) for k, r in results if k.startswith("recipe_"))
            assert recipe_kind == "recipe_api_error"
            assert recipe_error.code == "RECIPE_REVISION_INVALID_INGREDIENTS"
    finally:
        verify_db.close()
        _cleanup_recipe_graph(real_session_factory, product_id, ingredient_id, business_id)


def test_ingredient_hard_delete_races_recipe_creation_without_producing_an_invalid_state(
    real_session_factory,
):
    """Concise reuse of the same race infrastructure for hard-delete (referenced Ingredient
    hard-delete is also part of Phase 4's supported lifecycle, §4)."""
    setup_db = real_session_factory()
    try:
        business = make_business_graph(setup_db)
        product = make_product(setup_db, business)
        ingredient = make_ingredient(setup_db, business)
        setup_db.commit()
        business_id, product_id = business.id, product.id
        ingredient_id, ingredient_version = ingredient.id, ingredient.version
    finally:
        setup_db.close()

    results = _run_recipe_creation_vs_ingredient_mutation_race(
        real_session_factory,
        business_id,
        product_id,
        ingredient_id,
        ingredient_version,
        ingredient_service.delete_ingredient,
    )
    recipe_outcome = [r for kind, r in results if kind.startswith("recipe_")]
    mutate_outcome = [r for kind, r in results if kind.startswith("mutate_")]
    assert len(recipe_outcome) == 1
    assert len(mutate_outcome) == 1

    verify_db = real_session_factory()
    try:
        recipe_row = verify_db.scalar(select(Recipe).where(Recipe.product_id == product_id))
        recipe_kind = next(k for k, _ in results if k.startswith("recipe_"))
        mutate_kind = next(k for k, _ in results if k.startswith("mutate_"))
        if recipe_kind == "recipe_ok":
            # Recipe was created first — the ingredient is now referenced, so the
            # concurrent delete must have been correctly blocked, not silently succeeded.
            assert mutate_kind == "mutate_api_error"
            assert recipe_row is not None
        else:
            # Ingredient was deleted first — recipe creation correctly saw it as missing.
            assert mutate_kind == "mutate_ok"
            recipe_error = next(r for k, r in results if k == "recipe_api_error")
            assert recipe_error.code == "RECIPE_REVISION_INVALID_INGREDIENTS"
            assert recipe_row is None
    finally:
        verify_db.close()
        _cleanup_recipe_graph(real_session_factory, product_id, None, business_id)


def _cleanup_recipe_graph(real_session_factory, product_id, ingredient_id, business_id):
    from app.db.models.product import Product
    from app.db.models.user import User

    cleanup = real_session_factory()
    try:
        recipe_row = cleanup.scalar(select(Recipe).where(Recipe.product_id == product_id))
        if recipe_row is not None:
            cleanup.delete(recipe_row)
            cleanup.flush()
        if ingredient_id is not None:
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
