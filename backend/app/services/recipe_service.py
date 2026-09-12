"""Recipe / RecipeRevision / RecipeRevisionIngredient business logic
(Phase 4 Plan v4 §5-§11).

Recipe content is represented through immutable numbered RecipeRevisions (REC-002/007) —
"editing" a recipe never mutates an existing revision; it creates a new one and switches
`is_current`. Two distinct concurrency mechanisms protect this, neither a version column
(Recipe/RecipeRevision carry none):

1. A real PostgreSQL row lock (`SELECT ... FOR UPDATE`) on the Recipe (replacement
   revisions) or Product (first-Recipe creation) serializes every workflow that could race
   on that row — including, critically, first-Recipe creation racing against Product
   deletion (Phase 4 Plan v4 §6c; see `product_service.get_product_for_business_locked`).
2. `RecipeRevisionCreateRequest.expected_current_revision_id` — captured by the client
   when its edit session began, never silently advanced — is compared against the actual
   current revision *under the Recipe lock*; a mismatch is `409 RECIPE_REVISION_CONFLICT`
   with no write, never a silent overwrite of a stale editor's intent (Phase 4 Plan v4 §6a).

Ingredient-line composition (existence/tenant/unit-family/active-state/archived-carry-
forward) is validated against rows locked via `ingredient_service.lock_ingredients_for_business`
(Phase 4 Plan v4 §6b) — closing the race where an Ingredient could be archived/deleted
between validation and commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from psycopg.errors import UniqueViolation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.tenant import (
    get_recipe_for_product,
    get_recipe_revision_for_recipe,
    lock_recipe_for_product,
)
from app.db.enums import ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.product import Product
from app.db.models.recipe import Recipe, RecipeRevision, RecipeRevisionIngredient
from app.domain.unit_conversion import UnknownUnitError, unit_family
from app.schemas.recipe import (
    RecipeCreateRequest,
    RecipeRevisionCreateRequest,
    RecipeRevisionIngredientCreateRequest,
)
from app.services import ingredient_service
from app.services.product_service import (
    get_product_for_business,
    get_product_for_business_locked,
)

_REVISION_NUMBER_CONSTRAINT = "uq_recipe_revisions_recipe_id_revision_number"
_RECIPE_IDENTITY_CONSTRAINT = "uq_recipes_business_id_product_id"


def _relationship_issue(index: int) -> dict:
    """Non-disclosing structured issue for a foreign/missing `ingredient_id` inside a
    request body (Phase 4 Plan v4 §9) — deliberately identical whether the id doesn't
    exist at all or belongs to another tenant; distinct from a URL-resource lookup, which
    stays a bare non-revealing 404."""
    return {
        "severity": "ERROR",
        "code": "INGREDIENT_NOT_FOUND",
        "message": "One of the referenced ingredients could not be found.",
        "field": f"ingredients[{index}].ingredient_id",
        "resource": "recipe_revision_ingredient",
        "details": {},
    }


def _validate_and_lock_ingredient_lines(
    db: Session,
    business: Business,
    lines: Sequence[RecipeRevisionIngredientCreateRequest],
    *,
    base_revision_ingredient_ids: set[uuid.UUID] | None,
) -> list[tuple[RecipeRevisionIngredientCreateRequest, Ingredient]]:
    """Validates every submitted ingredient line against row-locked, authoritative
    Ingredient data (Phase 4 Plan v4 §5/§6b). `base_revision_ingredient_ids` is `None` for
    first-Recipe creation (every ingredient must be active) or the base revision's own
    ingredient-id set for a replacement revision (an archived ingredient is allowed only
    if it was already present there). Collects every invalid line into one `422` — never
    stops at the first — and writes nothing regardless of outcome."""
    ingredient_ids = [line.ingredient_id for line in lines]
    locked = ingredient_service.lock_ingredients_for_business(db, ingredient_ids, business)

    issues: list[dict] = []
    resolved: list[tuple[RecipeRevisionIngredientCreateRequest, Ingredient]] = []
    for index, line in enumerate(lines):
        ingredient_id = line.ingredient_id
        ingredient = locked.get(ingredient_id)
        if ingredient is None:
            issues.append(_relationship_issue(index))
            continue

        if not ingredient.is_active:
            carried_forward = (
                base_revision_ingredient_ids is not None
                and ingredient_id in base_revision_ingredient_ids
            )
            if not carried_forward:
                issues.append(
                    {
                        "severity": "ERROR",
                        "code": "INGREDIENT_NOT_ACTIVE",
                        "message": (
                            "This ingredient is archived and cannot be used in a new recipe line."
                        ),
                        "field": f"ingredients[{index}].ingredient_id",
                        "resource": "recipe_revision_ingredient",
                        "details": {},
                    }
                )
                continue

        try:
            line_family = unit_family(line.unit)
        except UnknownUnitError:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "INVALID_UNIT",
                    "message": f"{line.unit!r} is not a recognized unit.",
                    "field": f"ingredients[{index}].unit",
                    "resource": "recipe_revision_ingredient",
                    "details": {},
                }
            )
            continue

        if line_family is not ingredient.measurement_family:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "CROSS_FAMILY_UNIT_CONVERSION",
                    "message": (
                        f"{line.unit!r} is not compatible with this ingredient's "
                        "measurement family."
                    ),
                    "field": f"ingredients[{index}].unit",
                    "resource": "recipe_revision_ingredient",
                    "details": {},
                }
            )
            continue

        resolved.append((line, ingredient))

    if issues:
        raise ApiError(
            422,
            "RECIPE_REVISION_INVALID_INGREDIENTS",
            "One or more ingredient lines are invalid.",
            issues=issues,
        )
    return resolved


def get_recipe_with_current_revision(
    db: Session, business: Business, product: Product
) -> tuple[Recipe, RecipeRevision]:
    """Read path (Phase 4 Plan v4 §5 point 9) — raises `NotFoundError` (via
    `get_recipe_for_product`) when the Product has no Recipe yet, a normal, expected state
    for a fresh Produced Product (PRD-006), unambiguous because the frontend only calls
    this after the Product itself already fetched successfully. `.one()` on the current-
    revision query surfaces a broken zero/multiple-current invariant as an unhandled
    `NoResultFound`/`MultipleResultsFound` — the app's existing generic-500 path, never a
    silently-picked revision."""
    recipe = get_recipe_for_product(db, product)
    current = db.scalars(
        select(RecipeRevision).where(
            RecipeRevision.recipe_id == recipe.id,
            RecipeRevision.business_id == recipe.business_id,
            RecipeRevision.is_current == True,  # noqa: E712 - SQLAlchemy column comparison
        )
    ).one()
    return recipe, current


def list_recipe_revisions_for_product(
    db: Session, business: Business, product: Product, *, limit: int = 50, offset: int = 0
) -> tuple[list[RecipeRevision], int]:
    """Paginated per ADR-101 (Phase 4 Plan v4 §7) — deterministic `revision_number`
    descending (most recent first)."""
    recipe = get_recipe_for_product(db, product)
    conditions = [RecipeRevision.recipe_id == recipe.id, RecipeRevision.business_id == business.id]
    total = db.scalar(select(func.count()).select_from(RecipeRevision).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(RecipeRevision)
            .where(*conditions)
            .order_by(RecipeRevision.revision_number.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def get_recipe_revision_detail(
    db: Session, business: Business, product: Product, revision_id: uuid.UUID
) -> RecipeRevision:
    recipe = get_recipe_for_product(db, product)
    return get_recipe_revision_for_recipe(db, recipe, revision_id)


def create_recipe(
    db: Session, business: Business, product_id: uuid.UUID, payload: RecipeCreateRequest
) -> tuple[Recipe, RecipeRevision]:
    """Atomic Recipe + Revision 1 + all ingredient lines, one transaction, one commit
    (Phase 4 Plan v4 §5 point 2). The Product row is locked first (Phase 4 Plan v4 §6c) —
    before checking `product_type` and before checking whether a Recipe already exists —
    so this cannot race with a concurrent Product deletion; see
    `get_product_for_business_locked`'s docstring for the exact race this closes."""
    product = get_product_for_business_locked(db, product_id, business)

    if product.product_type is not ProductType.PRODUCED:
        db.rollback()
        raise ApiError(
            422,
            "RECIPE_REQUIRES_PRODUCED_PRODUCT",
            "Only Produced products can have a recipe.",
        )

    existing = db.scalar(
        select(Recipe).where(Recipe.product_id == product.id, Recipe.business_id == business.id)
    )
    if existing is not None:
        db.rollback()
        raise ApiError(
            409,
            "RECIPE_ALREADY_EXISTS",
            "This product already has a recipe.",
        )

    try:
        resolved_lines = _validate_and_lock_ingredient_lines(
            db, business, payload.ingredients, base_revision_ingredient_ids=None
        )
    except ApiError:
        # The Product row lock (and any Ingredient row locks already acquired inside the
        # validation call) must not be held past this expected rejection — explicit
        # rollback releases them before the 422 propagates. Only ApiError is caught here;
        # an unexpected exception is never swallowed.
        db.rollback()
        raise

    recipe = Recipe(
        id=uuid.uuid4(), business_id=business.id, product_id=product.id, name=payload.name
    )
    db.add(recipe)

    revision = RecipeRevision(
        id=uuid.uuid4(),
        business_id=business.id,
        recipe_id=recipe.id,
        revision_number=1,
        yield_quantity=payload.yield_quantity,
        active_time_minutes=payload.active_time_minutes,
        elapsed_time_minutes=payload.elapsed_time_minutes,
        notes=payload.notes,
        is_current=True,
    )
    db.add(revision)

    for line, ingredient in resolved_lines:
        db.add(
            RecipeRevisionIngredient(
                id=uuid.uuid4(),
                business_id=business.id,
                recipe_revision_id=revision.id,
                ingredient_id=ingredient.id,
                quantity=line.quantity,
                unit=line.unit,
            )
        )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == _RECIPE_IDENTITY_CONSTRAINT
        ):
            # Defense-in-depth only: the Product lock above already makes the pre-check
            # race-free in normal operation.
            raise ApiError(
                409, "RECIPE_ALREADY_EXISTS", "This product already has a recipe."
            ) from None
        raise
    return recipe, revision


def rename_recipe(
    db: Session, business: Business, product_id: uuid.UUID, name: str
) -> tuple[Recipe, RecipeRevision]:
    """Container-metadata-only rename (Phase 4 Plan v4 §5 point 10, §7) — never touches
    revision content; no version/concurrency guard (no version column on `recipes`)."""
    product = get_product_for_business(db, product_id, business)
    recipe = get_recipe_for_product(db, product)
    recipe.name = name
    db.commit()
    _, current = get_recipe_with_current_revision(db, business, product)
    return recipe, current


def create_recipe_revision(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    payload: RecipeRevisionCreateRequest,
) -> RecipeRevision:
    """Creates a new immutable revision and atomically switches `is_current` (Phase 4
    Plan v4 §6a). Full procedure:
    1. Lock the Recipe row (serializes every concurrent replacement-revision attempt).
    2. Load the current revision(s) under that lock; exactly one is the designed path —
       zero or multiple is an internal invariant failure, rolled back and propagated to
       the generic-500 path, never silently repaired or picked (Phase 4 Plan v4 §6a
       Correction 2).
    3. Compare its id against `payload.expected_current_revision_id`; a mismatch is
       `409 RECIPE_REVISION_CONFLICT`, no write.
    4. Validate the full replacement content (still before any write).
    5. Flip the old current revision to `is_current = False` and flush — required before
       inserting the new `is_current = True` row, since `ix_recipe_revisions_one_current`
       is a non-deferrable partial unique index.
    6. Insert the new revision + its ingredient lines, single commit.
    """
    product = get_product_for_business(db, product_id, business)
    recipe = lock_recipe_for_product(db, product)

    current_revisions = list(
        db.scalars(
            select(RecipeRevision).where(
                RecipeRevision.recipe_id == recipe.id,
                RecipeRevision.business_id == recipe.business_id,
                RecipeRevision.is_current == True,  # noqa: E712
            )
        )
    )
    if len(current_revisions) != 1:
        db.rollback()
        raise RuntimeError(
            f"Recipe {recipe.id} has {len(current_revisions)} current revisions, expected exactly 1"
        )
    current = current_revisions[0]

    if current.id != payload.expected_current_revision_id:
        db.rollback()
        raise ApiError(
            409,
            "RECIPE_REVISION_CONFLICT",
            "This recipe changed since you started editing. Refresh the latest revision "
            "and review your changes before saving again.",
        )

    base_ingredient_ids = {line.ingredient_id for line in current.ingredients}
    try:
        resolved_lines = _validate_and_lock_ingredient_lines(
            db, business, payload.ingredients, base_revision_ingredient_ids=base_ingredient_ids
        )
    except ApiError:
        # The Recipe row lock (and any Ingredient row locks already acquired inside the
        # validation call) must not be held past this expected rejection — explicit
        # rollback releases them before the 422 propagates. Only ApiError is caught here;
        # an unexpected exception is never swallowed.
        db.rollback()
        raise

    next_number = (
        db.scalar(
            select(func.max(RecipeRevision.revision_number)).where(
                RecipeRevision.recipe_id == recipe.id,
                RecipeRevision.business_id == recipe.business_id,
            )
        )
        or 0
    ) + 1

    current.is_current = False
    db.flush()

    new_revision = RecipeRevision(
        id=uuid.uuid4(),
        business_id=business.id,
        recipe_id=recipe.id,
        revision_number=next_number,
        yield_quantity=payload.yield_quantity,
        active_time_minutes=payload.active_time_minutes,
        elapsed_time_minutes=payload.elapsed_time_minutes,
        notes=payload.notes,
        is_current=True,
    )
    db.add(new_revision)

    for line, ingredient in resolved_lines:
        db.add(
            RecipeRevisionIngredient(
                id=uuid.uuid4(),
                business_id=business.id,
                recipe_revision_id=new_revision.id,
                ingredient_id=ingredient.id,
                quantity=line.quantity,
                unit=line.unit,
            )
        )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == _REVISION_NUMBER_CONSTRAINT
        ):
            # Pure defense-in-depth: the Recipe lock above already makes this race-free in
            # normal operation (Phase 4 Plan v4 §6a).
            raise ApiError(
                409,
                "RECIPE_REVISION_CONFLICT",
                "This recipe changed since you started editing. Refresh the latest "
                "revision and review your changes before saving again.",
            ) from None
        raise
    return new_revision
