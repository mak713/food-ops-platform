"""Deterministic ingredient-requirement calculation (Spec §14.7; Phase 7 Plan v2 §4).

Pure composition of the existing, unchanged Phase 4/5 calculators
(`app.domain.unit_conversion.convert`, `app.domain.recipe_scaling.scale_ingredient_quantity`)
— no new arithmetic rule is introduced here, only the per-recipe-line orchestration
Spec §14.7 describes. No HTTP or database access (ADR-109).
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain import recipe_scaling, unit_conversion

_CONTEXT_PRECISION = 50


@dataclass(frozen=True)
class RecipeRevisionIngredientLine:
    """One `RecipeRevisionIngredient` row, plus the Ingredient's own canonical unit
    (needed for the conversion step, Spec §7.5/§14.7 — "all calculations normalize to
    the Ingredient's canonical unit")."""

    ingredient_id: uuid.UUID
    quantity_per_batch: Decimal
    unit: str
    ingredient_canonical_unit: str


@dataclass(frozen=True)
class IngredientRequirement:
    ingredient_id: uuid.UUID
    required_quantity_canonical: Decimal


def calculate_ingredient_requirements(
    lines: Sequence[RecipeRevisionIngredientLine], batches: int
) -> list[IngredientRequirement]:
    """`required_ingredient_quantity = canonical_quantity_per_batch × required_batches`
    (Spec §14.7), for every ingredient line on the pinned RecipeRevision. Raises
    `unit_conversion.UnknownUnitError`/`IncompatibleUnitFamilyError` exactly as
    `convert` does — the caller is responsible for validating unit/family compatibility
    ahead of this call, matching the same discipline `recipe_service` already applies
    when a RecipeRevision is created. `unit_conversion.convert`/`recipe_scaling.
    scale_ingredient_quantity` are each already isolated in their own
    `decimal.localcontext()`; this function wraps its own orchestration in the same
    isolation (ADR-109) for consistency and to remain safe against any future direct
    arithmetic added here."""
    results: list[IngredientRequirement] = []
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        for line in lines:
            canonical_per_batch = unit_conversion.convert(
                line.quantity_per_batch, line.unit, line.ingredient_canonical_unit
            )
            total = recipe_scaling.scale_ingredient_quantity(canonical_per_batch, batches)
            results.append(
                IngredientRequirement(
                    ingredient_id=line.ingredient_id, required_quantity_canonical=total
                )
            )
    return results
