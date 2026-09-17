"""Unit tests for ingredient-requirement calculation (Phase 7 Plan v2 §4)."""

from __future__ import annotations

import decimal
import uuid
from decimal import Decimal

import pytest

from app.domain.ingredient_requirements import (
    RecipeRevisionIngredientLine,
    calculate_ingredient_requirements,
)
from app.domain.unit_conversion import IncompatibleUnitFamilyError


def test_same_unit_no_conversion_needed():
    ingredient_id = uuid.uuid4()
    lines = [
        RecipeRevisionIngredientLine(
            ingredient_id=ingredient_id,
            quantity_per_batch=Decimal("500"),
            unit="g",
            ingredient_canonical_unit="g",
        )
    ]
    result = calculate_ingredient_requirements(lines, batches=3)
    assert len(result) == 1
    assert result[0].ingredient_id == ingredient_id
    assert result[0].required_quantity_canonical == Decimal("1500")


def test_converts_to_canonical_unit_before_scaling():
    lines = [
        RecipeRevisionIngredientLine(
            ingredient_id=uuid.uuid4(),
            quantity_per_batch=Decimal("1"),
            unit="kg",
            ingredient_canonical_unit="g",
        )
    ]
    result = calculate_ingredient_requirements(lines, batches=2)
    assert result[0].required_quantity_canonical == Decimal("2000")


def test_zero_batches_yields_zero_requirement():
    lines = [
        RecipeRevisionIngredientLine(
            ingredient_id=uuid.uuid4(),
            quantity_per_batch=Decimal("500"),
            unit="g",
            ingredient_canonical_unit="g",
        )
    ]
    result = calculate_ingredient_requirements(lines, batches=0)
    assert result[0].required_quantity_canonical == Decimal("0")


def test_cross_family_unit_rejected():
    lines = [
        RecipeRevisionIngredientLine(
            ingredient_id=uuid.uuid4(),
            quantity_per_batch=Decimal("1"),
            unit="cup",
            ingredient_canonical_unit="g",
        )
    ]
    with pytest.raises(IncompatibleUnitFamilyError):
        calculate_ingredient_requirements(lines, batches=1)


def test_result_unaffected_by_corrupted_ambient_decimal_context():
    """ADR-109: even though this module currently only composes already-isolated
    `unit_conversion.convert`/`recipe_scaling.scale_ingredient_quantity` calls, it
    wraps its own orchestration in an isolated `decimal.localcontext()` too, for
    consistency and to stay safe against any future direct arithmetic added here.
    Prove isolation by corrupting the ambient global context before calling."""
    lines = [
        RecipeRevisionIngredientLine(
            ingredient_id=uuid.uuid4(),
            quantity_per_batch=Decimal("123.456789"),
            unit="kg",
            ingredient_canonical_unit="g",
        )
    ]
    baseline = calculate_ingredient_requirements(lines, batches=7)

    original_ctx = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 2
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        corrupted = calculate_ingredient_requirements(lines, batches=7)
    finally:
        decimal.setcontext(original_ctx)

    assert baseline[0].required_quantity_canonical == corrupted[0].required_quantity_canonical
    assert corrupted[0].required_quantity_canonical == Decimal("864197.523")
