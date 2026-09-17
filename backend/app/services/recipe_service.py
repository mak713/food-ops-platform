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
from app.db.enums import OrderStatus, ProductType
from app.db.models.business import Business
from app.db.models.ingredient import Ingredient
from app.db.models.order import Order, OrderLine
from app.db.models.product import Product
from app.db.models.production import ProductionRequirement, ProductionRequirementOrder
from app.db.models.recipe import Recipe, RecipeRevision, RecipeRevisionIngredient
from app.db.models.surplus import SurplusInventory
from app.domain.unit_conversion import UnknownUnitError, unit_family
from app.schemas.recipe import (
    RecipeCreateRequest,
    RecipeRevisionAffectedOrderLine,
    RecipeRevisionCreateRequest,
    RecipeRevisionImpactRequired,
    RecipeRevisionIngredientCreateRequest,
)
from app.services import ingredient_service
from app.services.operational_recalculation_service import (
    get_production_locked_order_line_ids,
    recalculate_product_closure,
    revisions_participating_in_closure,
)
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


def _validate_locked_ingredient_lines(
    lines: Sequence[RecipeRevisionIngredientCreateRequest],
    locked: dict[uuid.UUID, Ingredient],
    *,
    base_revision_ingredient_ids: set[uuid.UUID] | None,
) -> list[tuple[RecipeRevisionIngredientCreateRequest, Ingredient]]:
    """Validates every submitted ingredient line against an ALREADY-locked,
    authoritative `{ingredient_id: Ingredient}` map (Phase 4 Plan v4 §5/§6b) —
    performs no locking of its own, so a caller that needs a larger lock set than
    just the submitted lines (e.g. Recipe migration's old-revision-union lock,
    Phase 7 Remediation Finding 7) can lock everything it needs in one pass, then
    validate this subset against the result. `base_revision_ingredient_ids` is
    `None` for first-Recipe creation (every ingredient must be active) or the base
    revision's own ingredient-id set for a replacement revision (an archived
    ingredient is allowed only if it was already present there). Collects every
    invalid line into one `422` — never stops at the first — and writes nothing
    regardless of outcome."""
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


def _validate_and_lock_ingredient_lines(
    db: Session,
    business: Business,
    lines: Sequence[RecipeRevisionIngredientCreateRequest],
    *,
    base_revision_ingredient_ids: set[uuid.UUID] | None,
) -> list[tuple[RecipeRevisionIngredientCreateRequest, Ingredient]]:
    """Thin wrapper: locks exactly the submitted lines' ingredient ids, then
    validates them (Phase 4 Plan v4 §5/§6b). Correct as-is for first-Recipe
    creation and for the plain, non-impact `create_recipe_revision` path — neither
    has a separate old-revision ingredient set that also needs locking. The
    replacement-revision impact path (Recipe migration) does NOT use this wrapper —
    it locks the full old-revision-union-new-revision ingredient set in one pass
    first (see `_create_recipe_revision_with_full_ingredient_lock`) and calls
    `_validate_locked_ingredient_lines` directly against that already-locked set,
    so a migration never performs two sequential ingredient lock acquisitions."""
    ingredient_ids = [line.ingredient_id for line in lines]
    locked = ingredient_service.lock_ingredients_for_business(db, ingredient_ids, business)
    return _validate_locked_ingredient_lines(
        lines, locked, base_revision_ingredient_ids=base_revision_ingredient_ids
    )


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


def _validate_recipe_creation_preconditions(
    db: Session, business: Business, product: Product
) -> None:
    """`product_type` and existence checks shared by `create_recipe` and
    `create_recipe_with_impact` (Phase 7) — the caller must already hold the Product
    row lock (Phase 4 Plan v4 §6c)."""
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


def _create_recipe_uncommitted(
    db: Session, business: Business, product: Product, payload: RecipeCreateRequest
) -> tuple[Recipe, RecipeRevision]:
    """Ingredient validation + Recipe/Revision-1/ingredient-line inserts, no commit.
    Shared, byte-for-byte, by `create_recipe` (Phase 4, unchanged behavior when no
    Phase 7 confirmed demand exists yet to migrate) and `create_recipe_with_impact`
    (Phase 7 Plan v2 §9)."""
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
    return recipe, revision


def _commit_recipe_creation(db: Session, recipe: Recipe, revision: RecipeRevision) -> None:
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


def create_recipe(
    db: Session, business: Business, product_id: uuid.UUID, payload: RecipeCreateRequest
) -> tuple[Recipe, RecipeRevision]:
    """Atomic Recipe + Revision 1 + all ingredient lines, one transaction, one commit
    (Phase 4 Plan v4 §5 point 2). The Product row is locked first (Phase 4 Plan v4 §6c) —
    before checking `product_type` and before checking whether a Recipe already exists —
    so this cannot race with a concurrent Product deletion; see
    `get_product_for_business_locked`'s docstring for the exact race this closes."""
    product = get_product_for_business_locked(db, product_id, business)
    _validate_recipe_creation_preconditions(db, business, product)
    recipe, revision = _create_recipe_uncommitted(db, business, product, payload)
    _commit_recipe_creation(db, recipe, revision)
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


def _load_recipe_and_current_revision(
    db: Session, business: Business, product: Product
) -> tuple[Recipe, RecipeRevision]:
    """Lock the Recipe row and load its sole current revision (Phase 4 Plan v4 §6a).
    Zero or multiple current revisions is an internal invariant failure, rolled back
    and propagated to the generic-500 path, never silently repaired or picked."""
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
    return recipe, current_revisions[0]


def _insert_recipe_revision(
    db: Session,
    business: Business,
    recipe: Recipe,
    current: RecipeRevision,
    payload: RecipeRevisionCreateRequest,
    resolved_lines: list[tuple[RecipeRevisionIngredientCreateRequest, Ingredient]],
) -> RecipeRevision:
    """Version-conflict check + flip `is_current` + insert the new revision and its
    ingredient lines, no commit, given already-validated `resolved_lines` — no
    locking or validation happens here. Shared by both the plain, non-impact
    `create_recipe_revision` path and the Phase 7 impact path, which resolve their
    `resolved_lines` via different ingredient-locking strategies (see
    `_create_recipe_revision_uncommitted` vs
    `_create_recipe_revision_with_full_ingredient_lock`) but insert identically."""
    if current.id != payload.expected_current_revision_id:
        db.rollback()
        raise ApiError(
            409,
            "RECIPE_REVISION_CONFLICT",
            "This recipe changed since you started editing. Refresh the latest revision "
            "and review your changes before saving again.",
        )

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
    return new_revision


def _create_recipe_revision_uncommitted(
    db: Session,
    business: Business,
    recipe: Recipe,
    current: RecipeRevision,
    payload: RecipeRevisionCreateRequest,
) -> RecipeRevision:
    """Validate + insert the new revision, no commit — locks only the submitted
    lines' ingredients (correct here: the plain, non-impact `create_recipe_revision`
    path never migrates existing demand off the old revision, so the old revision's
    ingredients need no lock for this call). Caller has already locked the Recipe
    row and loaded `current` via `_load_recipe_and_current_revision`. The Phase 7
    impact path uses `_create_recipe_revision_with_full_ingredient_lock` instead —
    see that function for why it needs a different, larger lock set acquired in one
    pass."""
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

    return _insert_recipe_revision(db, business, recipe, current, payload, resolved_lines)


def _lock_surplus_for_product(db: Session, business: Business, product: Product) -> None:
    """Locks every `SurplusInventory` lot for this Product (Phase 7 Final
    Remediation Correction Plan, Finding 4) — Recipe migration's
    `_migrate_affected_lines` -> `recalculate_product_closure` reads/writes
    `SurplusAllocation` rows against these same lots
    (`operational_recalculation_service._recalculate_produced_closure`), so they
    must be locked before that call, matching
    `order_lifecycle_service._acquire_surplus_and_purchased_locks`'s Surplus half
    exactly. No `PurchasedProductInventory` counterpart exists here — Recipe
    migration only ever applies to PRODUCED products (a Recipe cannot exist for a
    PURCHASED one)."""
    db.scalars(
        select(SurplusInventory)
        .where(
            SurplusInventory.product_id == product.id,
            SurplusInventory.business_id == business.id,
        )
        .order_by(SurplusInventory.id)
        .with_for_update()
    ).all()


def _create_recipe_revision_with_full_ingredient_lock(
    db: Session,
    business: Business,
    product: Product,
    recipe: Recipe,
    current: RecipeRevision,
    payload: RecipeRevisionCreateRequest,
) -> RecipeRevision:
    """Phase 7 Implementation Remediation Plan, Finding 7 (Amendment 3), completed by
    the Phase 7 Final Remediation Correction Plan, Finding 4 — the replacement-
    revision impact path's ingredient lock set must cover every RecipeRevision this
    Product's recalculation might still touch, not merely the immediately-current
    revision being replaced: `_resolve_pin`
    (`operational_recalculation_service.py`) can legitimately keep a retained
    demand line pinned to an OLDER, historical revision (Plan v2 §9), so a
    concurrent migration must also serialize against THAT revision's ingredients —
    otherwise it could race a closure rebuild still reading them. Uses
    `revisions_participating_in_closure`, the identical shared query
    `order_lifecycle_service._acquire_recipe_and_ingredient_locks` relies on for
    the same concern on the confirm/edit/cancel side, so there is exactly one
    definition of "every revision this Product's closure might still touch" —
    never two independently-maintained ones that could silently drift apart.

    Locked, together with the submitted NEW revision's own ingredients, in exactly
    ONE lock call, before validation — never two sequential lock acquisitions
    (locking new, then separately locking old), which would let a concurrent
    transaction interleave between them. First-Recipe creation has no old revision
    at all, so it correctly stays on `_create_recipe_revision_uncommitted`'s
    single-submitted-set lock via `_validate_and_lock_ingredient_lines`."""
    base_ingredient_ids = {line.ingredient_id for line in current.ingredients}
    participating_revision_ids = revisions_participating_in_closure(
        db, business, product, current_revision_id=current.id
    )
    participating_ingredient_ids: set[uuid.UUID] = set()
    if participating_revision_ids:
        participating_ingredient_ids = set(
            db.scalars(
                select(RecipeRevisionIngredient.ingredient_id).where(
                    RecipeRevisionIngredient.recipe_revision_id.in_(participating_revision_ids),
                    RecipeRevisionIngredient.business_id == business.id,
                )
            )
        )
    submitted_ingredient_ids = {line.ingredient_id for line in payload.ingredients}
    full_ingredient_ids = participating_ingredient_ids | submitted_ingredient_ids

    try:
        locked = ingredient_service.lock_ingredients_for_business(db, full_ingredient_ids, business)
        resolved_lines = _validate_locked_ingredient_lines(
            payload.ingredients, locked, base_revision_ingredient_ids=base_ingredient_ids
        )
    except ApiError:
        # Same discipline as `_create_recipe_revision_uncommitted`: the Recipe lock
        # and every Ingredient lock acquired above (the full participating-union-
        # new set) must not be held past this expected rejection.
        db.rollback()
        raise

    return _insert_recipe_revision(db, business, recipe, current, payload, resolved_lines)


def _commit_recipe_revision(db: Session, new_revision: RecipeRevision) -> None:
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


def create_recipe_revision(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    payload: RecipeRevisionCreateRequest,
) -> RecipeRevision:
    """Creates a new immutable revision and atomically switches `is_current` (Phase 4
    Plan v4 §6a): lock Recipe -> load current -> validate -> flip -> insert -> commit."""
    product = get_product_for_business(db, product_id, business)
    recipe, current = _load_recipe_and_current_revision(db, business, product)
    new_revision = _create_recipe_revision_uncommitted(db, business, recipe, current, payload)
    _commit_recipe_revision(db, new_revision)
    return new_revision


# --- Phase 7: Recipe/Recipe-Revision impact-choice protocol -------------------------
# (Plan v2 §9, corrected by Final Pre-Implementation Amendment §5/§7/§8.) Neither
# function below ever acquires an Order row lock — migration mutates only
# Product-scoped operational projection state, never an Order aggregate column
# (Final Architecture Lock §7's resolved lock-order contradiction).


def _find_affected_eligible_order_lines(
    db: Session, business: Business, product: Product
) -> list[tuple[OrderLine, Order]]:
    """Every eligible CONFIRMED, unstarted OrderLine currently linked to this
    Product's operational demand (Phase 7 Final Semantic & Precision Correction
    Plan, Finding 1; §15.11) — regardless of whether its `ProductionRequirement` is
    pinned to the immediately-current revision, an older historical revision
    (retained via a prior `future_only`), or is `INCOMPLETE_RECIPE`
    (`recipe_revision_id IS NULL`, from earlier missing-Recipe demand) —
    deliberately no filter on `recipe_revision_id` at all. Excludes any line
    covered by an active `IN_PRODUCTION` run. `apply_existing` migrates every one
    of these; `future_only` changes none of them. Shared by both
    `create_recipe_revision_with_impact` (replacement revisions, where demand may
    be pinned to ANY revision or be incomplete) and `create_recipe_with_impact`
    (first-Recipe creation, where only incomplete demand can possibly exist yet —
    this query is a strict, behavior-preserving generalization for that caller)."""
    rows = db.execute(
        select(OrderLine, Order)
        .join(Order, OrderLine.order_id == Order.id)
        .join(
            ProductionRequirementOrder,
            ProductionRequirementOrder.order_line_id == OrderLine.id,
        )
        .join(
            ProductionRequirement,
            ProductionRequirementOrder.production_requirement_id == ProductionRequirement.id,
        )
        .where(
            ProductionRequirement.product_id == product.id,
            ProductionRequirement.business_id == business.id,
            ProductionRequirementOrder.business_id == business.id,
            OrderLine.business_id == business.id,
            Order.business_id == business.id,
            Order.status == OrderStatus.CONFIRMED,
        )
    ).all()
    protected = get_production_locked_order_line_ids(db, business, product)
    return [(line, order) for line, order in rows if line.id not in protected]


def _migrate_affected_lines(
    db: Session,
    business: Business,
    product: Product,
    affected: list[tuple[OrderLine, Order]],
    *,
    business_today,
) -> None:
    """Deletes the stale `ProductionRequirementOrder` link for each affected line —
    so the recalculation pass below resolves it as newly-linked demand, freshly
    pinned to whatever is now `is_current` — then runs the standard Product-scoped
    recalculation, which rebuilds every other (unaffected) rebuildable row exactly
    as it already was (same pin, same key), and the affected rows under the new
    revision."""
    if affected:
        affected_line_ids = [line.id for line, _order in affected]
        db.query(ProductionRequirementOrder).filter(
            ProductionRequirementOrder.order_line_id.in_(affected_line_ids),
            ProductionRequirementOrder.business_id == business.id,
        ).delete(synchronize_session=False)
        db.flush()
    recalculate_product_closure(db, business, product, business_today=business_today)


def _impact_required_error(affected: list[tuple[OrderLine, Order]]) -> ApiError:
    """Matches the codebase's established convention for a structured warning that
    requires explicit resubmission (e.g. `payment_overage_error`) — a `422` `ApiError`,
    never a differently-shaped `200` body."""
    payload = RecipeRevisionImpactRequired(
        affected_order_lines=[
            RecipeRevisionAffectedOrderLine(
                order_id=str(order.id),
                order_line_id=str(line.id),
                product_id=str(line.product_id),
                demand_date=order.fulfillment_date.isoformat() if order.fulfillment_date else "",
            )
            for line, order in affected
        ]
    )
    return ApiError(
        422,
        "RECIPE_REVISION_IMPACT_REQUIRED",
        "This recipe has confirmed, unstarted demand that would be affected. Choose "
        "whether to apply this change to existing confirmed production or only to new "
        "demand.",
        issues=[
            {
                "severity": "WARNING",
                "code": "RECIPE_REVISION_IMPACT_REQUIRED",
                "message": (
                    "Confirmed, unstarted demand exists for this product that would be "
                    "affected by this change."
                ),
                "field": "apply_scope",
                "resource": "recipe_revision",
                "details": {
                    "options": ["apply_existing", "future_only"],
                    "affected_order_lines": [
                        line.model_dump() for line in payload.affected_order_lines
                    ],
                },
            }
        ],
    )


def create_recipe_revision_with_impact(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    payload: RecipeRevisionCreateRequest,
    *,
    business_today,
) -> RecipeRevision:
    """`apply_scope` is optional/absent, never silently defaulted (Final
    Pre-Implementation Amendment §8): if affected confirmed unstarted demand exists
    and no choice was submitted, performs NO mutation — not even the new revision
    itself — and raises the structured `RECIPE_REVISION_IMPACT_REQUIRED` error instead.
    The Product row is locked FIRST, before the Recipe (Phase 7 Implementation
    Remediation Plan, Finding 7 — `create_recipe_with_impact` already did this;
    the replacement-revision path did not, leaving it unserialized against
    confirmation/edit/cancel, which lock Product before Recipe too, Final
    Architecture Lock §B)."""
    product = get_product_for_business_locked(db, product_id, business)
    recipe, current = _load_recipe_and_current_revision(db, business, product)

    affected = _find_affected_eligible_order_lines(db, business, product)
    if affected and payload.apply_scope is None:
        db.rollback()
        raise _impact_required_error(affected)

    new_revision = _create_recipe_revision_with_full_ingredient_lock(
        db, business, product, recipe, current, payload
    )

    try:
        if affected and payload.apply_scope == "apply_existing":
            # Surplus locked here — AFTER the Recipe/Ingredient lock above, BEFORE
            # `_migrate_affected_lines` -> `recalculate_product_closure` reads/writes
            # `SurplusAllocation` rows against these lots (Phase 7 Final Remediation
            # Correction Plan, Finding 4; Final Architecture Lock §B: Product ->
            # Recipe -> Ingredient -> Surplus, never an Order lock in this path).
            _lock_surplus_for_product(db, business, product)
            _migrate_affected_lines(db, business, product, affected, business_today=business_today)
    except ApiError:
        # An expected recalculation/migration ApiError (e.g. an overflow guard, or
        # PRODUCTION_LOCKED_DEMAND_DATE_CONFLICT) must not leave the Product/Recipe/
        # Ingredient/Surplus locks acquired above held past this rejection, nor
        # leave the not-yet-committed new revision row pending (Finding 7, point 3).
        db.rollback()
        raise

    _commit_recipe_revision(db, new_revision)
    return new_revision


def create_recipe_with_impact(
    db: Session,
    business: Business,
    product_id: uuid.UUID,
    payload: RecipeCreateRequest,
    *,
    business_today,
) -> tuple[Recipe, RecipeRevision]:
    """First-Recipe creation with the same impact-choice protocol (Final
    Pre-Implementation Amendment §7). No `expected_current_revision_id` exists or is
    required for this case — the Product lock plus the re-asserted
    Recipe-does-not-exist check under it (Amendment §5) is the sole concurrency
    mechanism, exactly as ordinary first-Recipe creation already uses."""
    product = get_product_for_business_locked(db, product_id, business)
    _validate_recipe_creation_preconditions(db, business, product)

    affected = _find_affected_eligible_order_lines(db, business, product)
    if affected and payload.apply_scope is None:
        db.rollback()
        raise _impact_required_error(affected)

    recipe, revision = _create_recipe_uncommitted(db, business, product, payload)

    try:
        if affected and payload.apply_scope == "apply_existing":
            # Surplus locked here too (Finding 4) — first-Recipe creation has no old
            # revision, but `_migrate_affected_lines` -> `recalculate_product_closure`
            # still reads/writes SurplusAllocation rows for this Product exactly as
            # the replacement-revision path does.
            _lock_surplus_for_product(db, business, product)
            _migrate_affected_lines(db, business, product, affected, business_today=business_today)
    except ApiError:
        # Same discipline as `create_recipe_revision_with_impact` (Finding 7, point
        # 3) — an expected recalculation/migration ApiError must not leave the
        # Product/Ingredient/Surplus locks held, nor the not-yet-committed
        # Recipe/Revision-1 rows pending.
        db.rollback()
        raise

    _commit_recipe_creation(db, recipe, revision)
    return recipe, revision
