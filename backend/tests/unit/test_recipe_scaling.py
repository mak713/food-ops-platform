"""Unit tests for the deterministic RecipeScaling domain module (Phase 4 Plan v4 §3b/§14)."""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest

from app.domain.recipe_scaling import (
    WholeBatchPlan,
    calculate_whole_batch_plan,
    scale_ingredient_quantity,
)


def test_zero_demand_yields_zero_batches_output_and_excess():
    plan = calculate_whole_batch_plan(Decimal("0"), Decimal("12"))
    assert plan == WholeBatchPlan(0, Decimal("0"), Decimal("0"))


def test_demand_below_one_yield_requires_one_batch():
    plan = calculate_whole_batch_plan(Decimal("5"), Decimal("12"))
    assert plan.required_batches == 1
    assert plan.expected_output == Decimal("12")
    assert plan.expected_excess == Decimal("7")


def test_demand_exactly_equal_to_one_yield_requires_one_batch_no_excess():
    plan = calculate_whole_batch_plan(Decimal("12"), Decimal("12"))
    assert plan.required_batches == 1
    assert plan.expected_output == Decimal("12")
    assert plan.expected_excess == Decimal("0")


def test_demand_above_one_yield_rounds_up_to_next_whole_batch():
    plan = calculate_whole_batch_plan(Decimal("13"), Decimal("12"))
    assert plan.required_batches == 2
    assert plan.expected_output == Decimal("24")
    assert plan.expected_excess == Decimal("11")


def test_seven_demand_three_yield_requires_three_batches():
    """The exact regression case for the removed, incorrect `-(-demand // yield)` Decimal
    trick: Decimal `//` truncates toward zero, so that trick computed `-(-7 // 3)` = 2 —
    silently wrong. The correct ceiling of 7/3 is 3."""
    plan = calculate_whole_batch_plan(Decimal("7"), Decimal("3"))
    assert plan.required_batches == 3
    assert plan.expected_output == Decimal("9")
    assert plan.expected_excess == Decimal("2")


def test_large_decimal_values_within_frozen_quantity_precision():
    demand = Decimal("999999999999.999999")
    yield_ = Decimal("0.000001")
    plan = calculate_whole_batch_plan(demand, yield_)
    # demand / yield == 999999999999999999 exactly -> already a whole number, no rounding up.
    assert plan.required_batches == 999999999999999999
    assert plan.expected_output == demand
    assert plan.expected_excess == Decimal("0")


def test_small_decimal_values_within_frozen_quantity_precision():
    demand = Decimal("0.000001")
    yield_ = Decimal("999999999999.999999")
    plan = calculate_whole_batch_plan(demand, yield_)
    assert plan.required_batches == 1
    assert plan.expected_output == yield_
    assert plan.expected_excess == yield_ - demand


def test_negative_demand_rejected():
    with pytest.raises(ValueError):
        calculate_whole_batch_plan(Decimal("-1"), Decimal("12"))


@pytest.mark.parametrize("bad_yield", [Decimal("0"), Decimal("-1")])
def test_non_positive_yield_rejected(bad_yield):
    with pytest.raises(ValueError):
        calculate_whole_batch_plan(Decimal("10"), bad_yield)


def test_calculate_whole_batch_plan_is_correct_even_under_a_tiny_ambient_decimal_context():
    demand = Decimal("999999999999.999999")
    yield_ = Decimal("0.000001")
    with decimal.localcontext() as ambient:
        ambient.prec = 2
        plan = calculate_whole_batch_plan(demand, yield_)
    assert plan.required_batches == 999999999999999999
    assert plan.expected_output == demand


def test_calculate_whole_batch_plan_does_not_mutate_the_ambient_decimal_context():
    before = decimal.getcontext().prec
    calculate_whole_batch_plan(Decimal("7"), Decimal("3"))
    assert decimal.getcontext().prec == before


def test_scale_ingredient_quantity_multiplies_per_batch_by_batch_count():
    assert scale_ingredient_quantity(Decimal("200"), 3) == Decimal("600")


def test_scale_ingredient_quantity_zero_batches_yields_zero():
    assert scale_ingredient_quantity(Decimal("200"), 0) == Decimal("0")


def test_scale_ingredient_quantity_rejects_non_positive_per_batch_quantity():
    with pytest.raises(ValueError):
        scale_ingredient_quantity(Decimal("0"), 3)
    with pytest.raises(ValueError):
        scale_ingredient_quantity(Decimal("-1"), 3)


@pytest.mark.parametrize("bad_batches", [-1, 2.5, "3", True, False])
def test_scale_ingredient_quantity_rejects_invalid_batches(bad_batches):
    with pytest.raises(ValueError):
        scale_ingredient_quantity(Decimal("200"), bad_batches)


def test_scale_ingredient_quantity_is_correct_even_under_a_tiny_ambient_decimal_context():
    with decimal.localcontext() as ambient:
        ambient.prec = 2
        result = scale_ingredient_quantity(Decimal("123456789.123456"), 999)
    with decimal.localcontext() as reference_ctx:
        reference_ctx.prec = 50
        reference = Decimal("123456789.123456") * Decimal(999)
    assert result == reference


def test_scale_ingredient_quantity_does_not_mutate_the_ambient_decimal_context():
    before = decimal.getcontext().prec
    scale_ingredient_quantity(Decimal("200"), 3)
    assert decimal.getcontext().prec == before
