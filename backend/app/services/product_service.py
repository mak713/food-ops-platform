"""Product and SellingOption CRUD, archive/reactivate/safe-delete business logic
(Phase 3 plan v3 §5/§7/§9/§10/§13).

Covers both models in one module, mirroring `db/models/product.py`'s own grouping —
SellingOptions are never meaningfully managed independently of their Product. Follows the
same conventions as `customer_service.py`/`auth_service.py`.
"""

from __future__ import annotations

import uuid

from psycopg.errors import ForeignKeyViolation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.api_errors import ApiError
from app.core.tenant import (
    NotFoundError,
    check_version,
    commit_or_raise_stale,
    get_owned_or_404,
    get_selling_option_for_product,
    stale_version_error,
)
from app.db.models.business import Business
from app.db.models.product import Product, SellingOption
from app.db.models.recipe import Recipe
from app.schemas.product import (
    ProductCreateRequest,
    ProductUpdateRequest,
    SellingOptionCreateRequest,
    SellingOptionUpdateRequest,
)

# --- Product -----------------------------------------------------------------------


def get_product_for_business(db: Session, product_id: uuid.UUID, business: Business) -> Product:
    return get_owned_or_404(db, Product, product_id, business)


def get_product_for_business_locked(
    db: Session, product_id: uuid.UUID, business: Business
) -> Product:
    """Tenant-scoped `SELECT ... FOR UPDATE` on the Product row (Phase 4 Plan v4 §6c) —
    an approved cross-phase concurrency hardening, not a Phase 3 feature change. Closes a
    race Phase 4 introduces: without this lock, `delete_product` could pre-check "no
    Recipe exists," a concurrent request could then create that Product's first Recipe,
    and the original deletion could still proceed, `CASCADE`-deleting the just-created
    Recipe. Both `delete_product` (below) and Phase 4's `create_recipe` acquire this same
    lock, in the same relative position (first, before anything that reads/depends on the
    Product's Recipe state), so the two workflows always serialize on this row instead of
    racing. Scoped by `(id, business_id)` in the query itself, matching ADR-099 exactly —
    never a bare `WHERE Product.id = ...`."""
    obj = db.scalar(
        select(Product)
        .where(Product.id == product_id, Product.business_id == business.id)
        .with_for_update()
    )
    if obj is None:
        raise NotFoundError()
    return obj


def create_product(db: Session, business: Business, payload: ProductCreateRequest) -> Product:
    """Creates one Product only — no nested Selling Options (Phase 3 plan v3 §9). Selling
    Options are always added afterward via their own dedicated endpoints, even for a
    brand-new Product with none yet; no minimum-Selling-Option-count is required by the
    spec."""
    product = Product(
        id=uuid.uuid4(),
        business_id=business.id,
        name=payload.name,
        product_type=payload.product_type,
        description=payload.description,
        default_packaging_cost=payload.default_packaging_cost,
        can_reuse_surplus=payload.can_reuse_surplus,
        default_surplus_usable_days=payload.default_surplus_usable_days,
    )
    db.add(product)
    db.commit()
    return product


def list_products_for_business(
    db: Session,
    business: Business,
    *,
    q: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Product], int]:
    conditions = [Product.business_id == business.id]
    if q:
        conditions.append(Product.name.ilike(f"%{q}%"))
    if is_active is not None:
        conditions.append(Product.is_active == is_active)

    total = db.scalar(select(func.count()).select_from(Product).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Product)
            .where(*conditions)
            .order_by(Product.name.asc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def update_product(
    db: Session, business: Business, product_id: uuid.UUID, payload: ProductUpdateRequest
) -> Product:
    """Product-level fields only — never `product_type` (immutable after creation, Phase 3
    plan v3 §10) and never `selling_options` (managed via their own endpoints, §9)."""
    product = get_product_for_business(db, product_id, business)
    check_version(product, payload.version, resource="product")

    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    for field, value in changes.items():
        setattr(product, field, value)
    if changes:
        commit_or_raise_stale(db, resource="product")
    return product


def archive_product(
    db: Session, business: Business, product_id: uuid.UUID, expected_version: int
) -> Product:
    """Never mutates any SellingOption.is_active (Phase 3 plan v3 §6) — each row's active
    flag stays independent of its parent's archived state."""
    product = get_product_for_business(db, product_id, business)
    check_version(product, expected_version, resource="product")
    if product.is_active:
        product.is_active = False
        commit_or_raise_stale(db, resource="product")
    return product


def reactivate_product(
    db: Session, business: Business, product_id: uuid.UUID, expected_version: int
) -> Product:
    product = get_product_for_business(db, product_id, business)
    check_version(product, expected_version, resource="product")
    if not product.is_active:
        product.is_active = True
        commit_or_raise_stale(db, resource="product")
    return product


def delete_product(
    db: Session, business: Business, product_id: uuid.UUID, expected_version: int
) -> None:
    """Safe-delete (Spec §8.32; Phase 3 plan v3 §5). `recipes.product_id` is the one
    `CASCADE` edge in the frozen schema that the database will NOT protect on its own —
    `Recipe` has no active/inactive concept (verified directly against the model), so this
    is an existence check, not an "active recipe" check. Every other reference into
    `products`/`selling_options` (order_lines, purchased_product_inventory,
    purchased_product_inventory_transactions, purchased_product_reservations,
    production_requirements, production_runs, surplus_inventory — all `ondelete="NO
    ACTION"`) is already enforced by the database itself; attempting the delete and
    catching the resulting foreign-key violation covers all of them without a
    hand-maintained per-table list. The DB's own `CASCADE` on `selling_options.product_id`
    then safely removes any (necessarily unreferenced, by the same NO ACTION guarantee)
    Selling Options atomically within the same statement.

    Phase 4 Plan v4 §6c (approved cross-phase concurrency hardening): the Product row is
    locked first, before the version check and the Recipe-existence check below, so a
    concurrent first-Recipe creation (Phase 4) cannot slip a new Recipe in between this
    function's existence check and its actual delete — see
    `get_product_for_business_locked`'s own docstring for the full race this closes.
    Every external behavior below (stale version, PRODUCT_HAS_RECIPE, PRODUCT_HAS_REFERENCES,
    success) is unchanged from Phase 3 — only the upfront lock is new.

    Both expected-rejection paths below (stale version, existing Recipe) now explicitly
    roll back before raising — the Product row lock must not be held past an expected
    rejection while its transaction sits open. Only the expected `ApiError` is caught for
    the version check; an unexpected exception is never swallowed."""
    product = get_product_for_business_locked(db, product_id, business)
    try:
        check_version(product, expected_version, resource="product")
    except ApiError:
        db.rollback()
        raise

    if db.scalar(select(Recipe.id).where(Recipe.product_id == product.id).limit(1)) is not None:
        db.rollback()
        raise ApiError(
            409,
            "PRODUCT_HAS_RECIPE",
            "This product has a recipe and cannot be deleted. Archive it instead.",
        )

    try:
        db.delete(product)
        db.commit()
    except StaleDataError:
        db.rollback()
        raise stale_version_error(resource="product") from None
    except IntegrityError as exc:
        db.rollback()
        if isinstance(exc.orig, ForeignKeyViolation):
            raise ApiError(
                409,
                "PRODUCT_HAS_REFERENCES",
                "This product has operational history and cannot be deleted. Archive it instead.",
            ) from None
        raise


# --- SellingOption -------------------------------------------------------------------
# No single-resource get_selling_option (and no GET /products/{id}/selling-options/{id}
# route) — removed in the Checkpoint 3 remediation pass as unapproved by Phase 3 plan v3;
# the frontend never needed it. Selling Options are read via the embedded list on
# `ProductResponse`/`GET /products/{id}/selling-options`; each mutation function below
# still resolves its own tenant-scoped row via `get_selling_option_for_product` directly.


def create_selling_option(
    db: Session, product: Product, payload: SellingOptionCreateRequest
) -> SellingOption:
    """Not blocked by the parent Product's archived state (Phase 3 plan v3 §7) — editing a
    Selling Option is independent of the parent's operational availability."""
    option = SellingOption(
        id=uuid.uuid4(),
        business_id=product.business_id,
        product_id=product.id,
        name=payload.name,
        quantity_units=payload.quantity_units,
        price=payload.price,
        packaging_cost=payload.packaging_cost,
        sort_order=payload.sort_order,
    )
    db.add(option)
    db.commit()
    return option


def update_selling_option(
    db: Session, product: Product, option_id: uuid.UUID, payload: SellingOptionUpdateRequest
) -> SellingOption:
    option = get_selling_option_for_product(db, product, option_id)
    check_version(option, payload.version, resource="selling_option")

    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    for field, value in changes.items():
        setattr(option, field, value)
    if changes:
        commit_or_raise_stale(db, resource="selling_option")
    return option


def archive_selling_option(
    db: Session, product: Product, option_id: uuid.UUID, expected_version: int
) -> SellingOption:
    option = get_selling_option_for_product(db, product, option_id)
    check_version(option, expected_version, resource="selling_option")
    if option.is_active:
        option.is_active = False
        commit_or_raise_stale(db, resource="selling_option")
    return option


def reactivate_selling_option(
    db: Session, product: Product, option_id: uuid.UUID, expected_version: int
) -> SellingOption:
    option = get_selling_option_for_product(db, product, option_id)
    check_version(option, expected_version, resource="selling_option")
    if not option.is_active:
        option.is_active = True
        commit_or_raise_stale(db, resource="selling_option")
    return option


def delete_selling_option(
    db: Session, product: Product, option_id: uuid.UUID, expected_version: int
) -> None:
    """`order_lines.selling_option_id` (`NO ACTION`) is the only reference; no owned
    children, no CASCADE edge to pre-check (Phase 3 plan v3 §5)."""
    option = get_selling_option_for_product(db, product, option_id)
    check_version(option, expected_version, resource="selling_option")

    try:
        db.delete(option)
        db.commit()
    except StaleDataError:
        db.rollback()
        raise stale_version_error(resource="selling_option") from None
    except IntegrityError as exc:
        db.rollback()
        if isinstance(exc.orig, ForeignKeyViolation):
            raise ApiError(
                409,
                "SELLING_OPTION_HAS_REFERENCES",
                "This selling option has order history and cannot be deleted. Archive it instead.",
            ) from None
        raise
