"""Ingredient CRUD/list/archive/reactivate/delete/immutability — Phase 4 Plan v4 §4/§14."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models.ingredient import Ingredient
from tests.integration.api.helpers import csrf_headers, signup
from tests.integration.schema.factories import (
    make_product,
    make_recipe,
    make_recipe_revision,
    make_recipe_revision_ingredient,
)


def _create_ingredient(client, **overrides):
    signup(client)
    payload = {"name": "Flour", "measurement_family": "WEIGHT", "canonical_unit": "g"}
    payload.update(overrides)
    return client.post("/api/v1/ingredients", json=payload, headers=csrf_headers(client))


def test_create_ingredient(client):
    response = _create_ingredient(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Flour"
    assert body["measurement_family"] == "WEIGHT"
    assert body["canonical_unit"] == "g"
    assert body["is_active"] is True
    assert body["version"] == 1
    # No Phase 5 cost/quantity fields anywhere in the response (Phase 4 Plan v4 §4).
    assert "physical_quantity" not in body
    assert "weighted_average_unit_cost" not in body
    assert "latest_purchase_unit_cost" not in body
    assert "replacement_unit_cost" not in body


def test_create_ingredient_in_each_measurement_family(client):
    signup(client)
    combos = [
        ("WEIGHT", "kg"),
        ("VOLUME", "L"),
        ("COUNT", "each"),
    ]
    for family, unit in combos:
        response = client.post(
            "/api/v1/ingredients",
            json={"name": f"{family} item", "measurement_family": family, "canonical_unit": unit},
            headers=csrf_headers(client),
        )
        assert response.status_code == 201, (family, unit, response.text)


def test_create_ingredient_blank_name_rejected(client):
    signup(client)
    response = client.post(
        "/api/v1/ingredients",
        json={"name": "   ", "measurement_family": "WEIGHT", "canonical_unit": "g"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_create_ingredient_canonical_unit_must_match_measurement_family(client):
    signup(client)
    response = client.post(
        "/api/v1/ingredients",
        json={"name": "Milk", "measurement_family": "WEIGHT", "canonical_unit": "mL"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_create_ingredient_unknown_unit_rejected(client):
    signup(client)
    response = client.post(
        "/api/v1/ingredients",
        json={"name": "Mystery", "measurement_family": "WEIGHT", "canonical_unit": "gallon"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_list_ingredients_search_and_pagination(client):
    signup(client)
    for name in ["Alpha Flour", "Beta Sugar", "Gamma Butter"]:
        client.post(
            "/api/v1/ingredients",
            json={"name": name, "measurement_family": "WEIGHT", "canonical_unit": "g"},
            headers=csrf_headers(client),
        )

    response = client.get("/api/v1/ingredients?q=Beta")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Beta Sugar"

    paged = client.get("/api/v1/ingredients?limit=2&offset=0")
    assert paged.json()["total"] == 3
    assert len(paged.json()["items"]) == 2
    names = [i["name"] for i in paged.json()["items"]]
    assert names == sorted(names)


def test_list_ingredients_is_active_filter(client):
    created = _create_ingredient(client).json()
    client.post(
        f"/api/v1/ingredients/{created['id']}/archive",
        json={"version": created["version"]},
        headers=csrf_headers(client),
    )

    active_only = client.get("/api/v1/ingredients?is_active=true")
    assert active_only.json()["total"] == 0

    archived_only = client.get("/api/v1/ingredients?is_active=false")
    assert archived_only.json()["total"] == 1


def test_get_ingredient(client):
    created = _create_ingredient(client).json()
    response = client.get(f"/api/v1/ingredients/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_update_ingredient_name(client):
    created = _create_ingredient(client).json()
    response = client.patch(
        f"/api/v1/ingredients/{created['id']}",
        json={"version": created["version"], "name": "Bread Flour"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Bread Flour"
    assert body["version"] == 2


def test_update_ingredient_measurement_family_and_canonical_unit_are_immutable(client):
    created = _create_ingredient(client).json()
    response = client.patch(
        f"/api/v1/ingredients/{created['id']}",
        json={
            "version": created["version"],
            "name": "Renamed",
            "measurement_family": "VOLUME",
            "canonical_unit": "mL",
        },
        headers=csrf_headers(client),
    )
    # Pydantic ignores the unknown fields on IngredientUpdateRequest — only `name` applies.
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["measurement_family"] == "WEIGHT"
    assert body["canonical_unit"] == "g"


def test_update_ingredient_explicit_null_name_rejected(client):
    created = _create_ingredient(client).json()
    response = client.patch(
        f"/api/v1/ingredients/{created['id']}",
        json={"version": created["version"], "name": None},
        headers=csrf_headers(client),
    )
    assert response.status_code == 422


def test_archive_and_reactivate_ingredient_is_idempotent(client):
    created = _create_ingredient(client).json()
    ingredient_id = created["id"]

    archived = client.post(
        f"/api/v1/ingredients/{ingredient_id}/archive",
        json={"version": created["version"]},
        headers=csrf_headers(client),
    )
    assert archived.status_code == 200
    assert archived.json()["is_active"] is False
    assert archived.json()["version"] == 2

    archived_again = client.post(
        f"/api/v1/ingredients/{ingredient_id}/archive",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert archived_again.status_code == 200
    assert archived_again.json()["version"] == 2

    reactivated = client.post(
        f"/api/v1/ingredients/{ingredient_id}/reactivate",
        json={"version": 2},
        headers=csrf_headers(client),
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    assert reactivated.json()["version"] == 3

    reactivated_again = client.post(
        f"/api/v1/ingredients/{ingredient_id}/reactivate",
        json={"version": 3},
        headers=csrf_headers(client),
    )
    assert reactivated_again.status_code == 200
    assert reactivated_again.json()["version"] == 3


def test_stale_version_on_ingredient_update_returns_409(client):
    created = _create_ingredient(client).json()
    response = client.patch(
        f"/api/v1/ingredients/{created['id']}",
        json={"version": created["version"] + 1, "name": "New Name"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "STALE_VERSION"
    assert "changed since you opened it" in body["error"]["message"]


def test_stale_version_on_ingredient_archive_returns_409(client):
    created = _create_ingredient(client).json()
    response = client.post(
        f"/api/v1/ingredients/{created['id']}/archive",
        json={"version": created["version"] + 1},
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"


def test_delete_unreferenced_ingredient_succeeds(client):
    created = _create_ingredient(client).json()
    response = client.delete(
        f"/api/v1/ingredients/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 204

    follow_up = client.get(f"/api/v1/ingredients/{created['id']}")
    assert follow_up.status_code == 404


def test_delete_referenced_ingredient_is_blocked(client, session):
    created = _create_ingredient(client).json()

    ingredient_row = session.execute(
        select(Ingredient).where(Ingredient.id == created["id"])
    ).scalar_one()

    product = make_product(session, ingredient_row.business)
    recipe = make_recipe(session, ingredient_row.business, product)
    revision = make_recipe_revision(session, ingredient_row.business, recipe)
    make_recipe_revision_ingredient(session, ingredient_row.business, revision, ingredient_row)
    session.commit()

    response = client.delete(
        f"/api/v1/ingredients/{created['id']}?version={created['version']}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INGREDIENT_HAS_REFERENCES"


def test_delete_ingredient_stale_version_is_distinct_from_reference_conflict(client):
    created = _create_ingredient(client).json()
    response = client.delete(
        f"/api/v1/ingredients/{created['id']}?version={created['version'] + 1}",
        headers=csrf_headers(client),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_VERSION"
