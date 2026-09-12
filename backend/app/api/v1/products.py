"""Product CRUD/list/archive/reactivate/delete, and nested Selling Option endpoints
(Phase 3 plan v3 §13).

`POST /products` creates one Product only — no nested Selling Options (Phase 3 plan v3
§9). Selling Options are managed exclusively via their own nested routes under a Product,
for both a brand-new (possibly empty) Product and an existing one alike.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_business, require_csrf
from app.db.models.business import Business
from app.db.models.product import Product, SellingOption
from app.db.models.recipe import Recipe, RecipeRevision, RecipeRevisionIngredient
from app.db.session import get_db
from app.schemas.common import PageResponse
from app.schemas.product import (
    LifecycleActionRequest,
    ProductCreateRequest,
    ProductResponse,
    ProductSummary,
    ProductUpdateRequest,
    SellingOptionCreateRequest,
    SellingOptionResponse,
    SellingOptionUpdateRequest,
)
from app.schemas.recipe import (
    RecipeCreateRequest,
    RecipeRenameRequest,
    RecipeResponse,
    RecipeRevisionCreateRequest,
    RecipeRevisionIngredientResponse,
    RecipeRevisionResponse,
    RecipeRevisionSummary,
)
from app.services import product_service, recipe_service

router = APIRouter()


def _selling_option_to_response(option: SellingOption) -> SellingOptionResponse:
    return SellingOptionResponse(
        id=str(option.id),
        product_id=str(option.product_id),
        name=option.name,
        quantity_units=option.quantity_units,
        price=option.price,
        packaging_cost=option.packaging_cost,
        sort_order=option.sort_order,
        is_active=option.is_active,
        version=option.version,
    )


def _product_to_response(product: Product) -> ProductResponse:
    return ProductResponse(
        id=str(product.id),
        name=product.name,
        description=product.description,
        product_type=product.product_type,
        default_packaging_cost=product.default_packaging_cost,
        can_reuse_surplus=product.can_reuse_surplus,
        default_surplus_usable_days=product.default_surplus_usable_days,
        is_active=product.is_active,
        version=product.version,
        selling_options=[_selling_option_to_response(o) for o in product.selling_options],
    )


def _product_to_summary(product: Product) -> ProductSummary:
    return ProductSummary(
        id=str(product.id),
        name=product.name,
        product_type=product.product_type,
        is_active=product.is_active,
        version=product.version,
    )


def _revision_ingredient_to_response(
    line: RecipeRevisionIngredient,
) -> RecipeRevisionIngredientResponse:
    return RecipeRevisionIngredientResponse(
        id=str(line.id),
        ingredient_id=str(line.ingredient_id),
        ingredient_name=line.ingredient.name,
        ingredient_is_active=line.ingredient.is_active,
        quantity=line.quantity,
        unit=line.unit,
    )


def _revision_to_response(revision: RecipeRevision) -> RecipeRevisionResponse:
    return RecipeRevisionResponse(
        id=str(revision.id),
        recipe_id=str(revision.recipe_id),
        revision_number=revision.revision_number,
        yield_quantity=revision.yield_quantity,
        active_time_minutes=revision.active_time_minutes,
        elapsed_time_minutes=revision.elapsed_time_minutes,
        notes=revision.notes,
        is_current=revision.is_current,
        ingredients=[_revision_ingredient_to_response(line) for line in revision.ingredients],
    )


def _revision_to_summary(revision: RecipeRevision) -> RecipeRevisionSummary:
    return RecipeRevisionSummary(
        id=str(revision.id),
        revision_number=revision.revision_number,
        yield_quantity=revision.yield_quantity,
        is_current=revision.is_current,
    )


def _recipe_to_response(recipe: Recipe, current_revision: RecipeRevision) -> RecipeResponse:
    return RecipeResponse(
        id=str(recipe.id),
        product_id=str(recipe.product_id),
        name=recipe.name,
        current_revision=_revision_to_response(current_revision),
    )


# --- Product ---------------------------------------------------------------------


@router.get("")
def list_products(
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    q: str | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[ProductSummary]:
    items, total = product_service.list_products_for_business(
        db, business, q=q, is_active=is_active, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_product_to_summary(p) for p in items], total=total, limit=limit, offset=offset
    )


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
def create_product(
    payload: ProductCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductResponse:
    product = product_service.create_product(db, business, payload)
    return _product_to_response(product)


@router.get("/{product_id}")
def get_product(
    product_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    return _product_to_response(product)


@router.patch("/{product_id}", dependencies=[Depends(require_csrf)])
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductResponse:
    product = product_service.update_product(db, business, product_id, payload)
    return _product_to_response(product)


@router.post("/{product_id}/archive", dependencies=[Depends(require_csrf)])
def archive_product(
    product_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductResponse:
    product = product_service.archive_product(db, business, product_id, payload.version)
    return _product_to_response(product)


@router.post("/{product_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_product(
    product_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductResponse:
    product = product_service.reactivate_product(db, business, product_id, payload.version)
    return _product_to_response(product)


@router.delete("/{product_id}", status_code=204, dependencies=[Depends(require_csrf)])
def delete_product(
    product_id: uuid.UUID,
    version: int,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    product_service.delete_product(db, business, product_id, version)


# --- SellingOption (nested under a Product) ---------------------------------------


@router.get("/{product_id}/selling-options")
def list_selling_options(
    product_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> list[SellingOptionResponse]:
    product = product_service.get_product_for_business(db, product_id, business)
    return [_selling_option_to_response(o) for o in product.selling_options]


@router.post("/{product_id}/selling-options", status_code=201, dependencies=[Depends(require_csrf)])
def create_selling_option(
    product_id: uuid.UUID,
    payload: SellingOptionCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> SellingOptionResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    option = product_service.create_selling_option(db, product, payload)
    return _selling_option_to_response(option)


@router.patch("/{product_id}/selling-options/{option_id}", dependencies=[Depends(require_csrf)])
def update_selling_option(
    product_id: uuid.UUID,
    option_id: uuid.UUID,
    payload: SellingOptionUpdateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> SellingOptionResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    option = product_service.update_selling_option(db, product, option_id, payload)
    return _selling_option_to_response(option)


@router.post(
    "/{product_id}/selling-options/{option_id}/archive",
    dependencies=[Depends(require_csrf)],
)
def archive_selling_option(
    product_id: uuid.UUID,
    option_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> SellingOptionResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    option = product_service.archive_selling_option(db, product, option_id, payload.version)
    return _selling_option_to_response(option)


@router.post(
    "/{product_id}/selling-options/{option_id}/reactivate",
    dependencies=[Depends(require_csrf)],
)
def reactivate_selling_option(
    product_id: uuid.UUID,
    option_id: uuid.UUID,
    payload: LifecycleActionRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> SellingOptionResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    option = product_service.reactivate_selling_option(db, product, option_id, payload.version)
    return _selling_option_to_response(option)


@router.delete(
    "/{product_id}/selling-options/{option_id}",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
def delete_selling_option(
    product_id: uuid.UUID,
    option_id: uuid.UUID,
    version: int,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    product = product_service.get_product_for_business(db, product_id, business)
    product_service.delete_selling_option(db, product, option_id, version)


# --- Recipe (nested under a Product, 1:1) -------------------------------------------


@router.get("/{product_id}/recipe")
def get_recipe(
    product_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> RecipeResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    recipe, current = recipe_service.get_recipe_with_current_revision(db, business, product)
    return _recipe_to_response(recipe, current)


@router.post("/{product_id}/recipe", status_code=201, dependencies=[Depends(require_csrf)])
def create_recipe(
    product_id: uuid.UUID,
    payload: RecipeCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> RecipeResponse:
    recipe, revision = recipe_service.create_recipe(db, business, product_id, payload)
    return _recipe_to_response(recipe, revision)


@router.patch("/{product_id}/recipe", dependencies=[Depends(require_csrf)])
def rename_recipe(
    product_id: uuid.UUID,
    payload: RecipeRenameRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> RecipeResponse:
    recipe, current = recipe_service.rename_recipe(db, business, product_id, payload.name)
    return _recipe_to_response(recipe, current)


# --- Recipe Revisions (nested under a Recipe) ---------------------------------------


@router.get("/{product_id}/recipe/revisions")
def list_recipe_revisions(
    product_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageResponse[RecipeRevisionSummary]:
    product = product_service.get_product_for_business(db, product_id, business)
    items, total = recipe_service.list_recipe_revisions_for_product(
        db, business, product, limit=limit, offset=offset
    )
    return PageResponse(
        items=[_revision_to_summary(r) for r in items], total=total, limit=limit, offset=offset
    )


@router.get("/{product_id}/recipe/revisions/{revision_id}")
def get_recipe_revision(
    product_id: uuid.UUID,
    revision_id: uuid.UUID,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> RecipeRevisionResponse:
    product = product_service.get_product_for_business(db, product_id, business)
    revision = recipe_service.get_recipe_revision_detail(db, business, product, revision_id)
    return _revision_to_response(revision)


@router.post(
    "/{product_id}/recipe/revisions", status_code=201, dependencies=[Depends(require_csrf)]
)
def create_recipe_revision(
    product_id: uuid.UUID,
    payload: RecipeRevisionCreateRequest,
    business: Annotated[Business, Depends(get_current_business)],
    db: Annotated[Session, Depends(get_db)],
) -> RecipeRevisionResponse:
    revision = recipe_service.create_recipe_revision(db, business, product_id, payload)
    return _revision_to_response(revision)
