"""Request/response schemas for Recipe, RecipeRevision, RecipeRevisionIngredient
(Phase 4 Plan v4 §5/§7/§11).

A RecipeRevision is an immutable content snapshot — "editing" a recipe means submitting
the *complete* new content, never a partial patch (unlike Customer/Product/SellingOption's
omitted-vs-null PATCH convention). `yield_quantity`/ingredient-line `quantity` are
constrained to the frozen `NUMERIC(18,6)` column shape via Pydantic's own `max_digits`/
`decimal_places` (Phase 4 Plan v4 §11) — verified against boundary cases in
`tests/integration/api/test_recipe_revisions.py`, not a hand-rolled digit-counting
fallback.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RecipeRevisionIngredientCreateRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    unit: str = Field(min_length=1, max_length=10)


class _RevisionContent(BaseModel):
    """Shared content fields for both the first revision (created together with the
    Recipe) and every replacement revision — factored out so both request shapes carry
    identical validation."""

    yield_quantity: Decimal = Field(max_digits=18, decimal_places=6, gt=0)
    active_time_minutes: int = Field(ge=0)
    elapsed_time_minutes: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    # Spec §22 Phase 4 "Recipe Revision Ingredients"; a revision with zero ingredient
    # lines is not a meaningful state (Phase 4 Plan v4 §5 point 3).
    ingredients: list[RecipeRevisionIngredientCreateRequest] = Field(min_length=1)

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _elapsed_at_least_active(self) -> _RevisionContent:
        if (
            self.elapsed_time_minutes is not None
            and self.elapsed_time_minutes < self.active_time_minutes
        ):
            raise ValueError("elapsed_time_minutes must be >= active_time_minutes.")
        return self

    @model_validator(mode="after")
    def _no_duplicate_ingredient_lines(self) -> _RevisionContent:
        """Pure application-level check (Phase 4 Plan v4 §4/§8) — there is no DB
        constraint on `(recipe_revision_id, ingredient_id)` to lean on; this must run
        here, before any database access."""
        ids = [line.ingredient_id for line in self.ingredients]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate ingredient_id values are not allowed within one revision.")
        return self


class RecipeCreateRequest(_RevisionContent):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name is required.")
        return stripped


class RecipeRevisionCreateRequest(_RevisionContent):
    """`expected_current_revision_id` is the exact revision the edit session was opened
    against (Phase 4 Plan v4 §6a) — never a live/derived value. A mismatch against the
    Recipe's actual current revision, checked under a row lock, produces
    `409 RECIPE_REVISION_CONFLICT` with no write."""

    expected_current_revision_id: uuid.UUID


class RecipeRenameRequest(BaseModel):
    """Container-metadata-only rename (Phase 4 Plan v4 §5 point 10) — never touches
    revision content, and needs no version/concurrency guard (no version column exists on
    `recipes`; low-risk, low-contention field)."""

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name is required.")
        return stripped


class RecipeRevisionIngredientResponse(BaseModel):
    id: str
    ingredient_id: str
    ingredient_name: str
    ingredient_is_active: bool
    quantity: Decimal
    unit: str


class RecipeRevisionResponse(BaseModel):
    id: str
    recipe_id: str
    revision_number: int
    yield_quantity: Decimal
    active_time_minutes: int
    elapsed_time_minutes: int | None
    notes: str | None
    is_current: bool
    ingredients: list[RecipeRevisionIngredientResponse]


class RecipeRevisionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    revision_number: int
    yield_quantity: Decimal
    is_current: bool


class RecipeResponse(BaseModel):
    id: str
    product_id: str
    name: str
    current_revision: RecipeRevisionResponse
