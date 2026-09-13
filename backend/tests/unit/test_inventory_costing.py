"""Unit tests for the deterministic inventory-costing domain module (Phase 5 Plan §G)."""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest

from app.domain.inventory_costing import (
    NUMERIC_18_6_MAX_MAGNITUDE,
    calculate_total_cost,
    calculate_weighted_average_restock,
    is_representable_in_numeric_18_6,
    quantize_for_storage,
    resolve_effective_replacement_cost,
)

# --- calculate_weighted_average_restock ----------------------------------------------


def test_normal_positive_balance_weighted_average():
    # 10 units @ $2.00 already on hand; restock 5 units @ $5.00.
    # (10*2 + 5*5) / (10+5) = (20+25)/15 = 45/15 = 3.00
    result = calculate_weighted_average_restock(
        old_quantity=Decimal("10"),
        old_weighted_average=Decimal("2.00"),
        purchased_quantity=Decimal("5"),
        purchase_unit_cost=Decimal("5.00"),
    )
    assert result == Decimal("3.00")


def test_zero_pre_restock_balance_resolves_to_purchase_cost():
    result = calculate_weighted_average_restock(
        old_quantity=Decimal("0"),
        old_weighted_average=Decimal("0"),
        purchased_quantity=Decimal("5"),
        purchase_unit_cost=Decimal("3.50"),
    )
    assert result == Decimal("3.50")


def test_negative_balance_restored_to_positive_resolves_to_purchase_cost_not_a_blend():
    # -10 units at a $2.00 basis, restocked with +15 units @ $3.00.
    # The raw §15.3 formula would give (-20+45)/5 = $5.00 — a cost higher than the
    # purchase price itself, which is not a truthful blend of anything real. The
    # approved rule discards the deficit's weight entirely instead.
    result = calculate_weighted_average_restock(
        old_quantity=Decimal("-10"),
        old_weighted_average=Decimal("2.00"),
        purchased_quantity=Decimal("15"),
        purchase_unit_cost=Decimal("3.00"),
    )
    assert result == Decimal("3.00")
    # Explicitly confirm this is NOT the raw formula's result.
    raw_formula_result = (Decimal("-10") * Decimal("2.00") + Decimal("15") * Decimal("3.00")) / (
        Decimal("-10") + Decimal("15")
    )
    assert result != raw_formula_result


def test_negative_balance_remaining_negative_after_partial_restock_resolves_to_purchase_cost():
    # -10 units, restocked with only +5 units @ $3.00 -> still -5 units after.
    # The raw formula would give (-20+15)/(-5) = $1.00 — arithmetic noise, not a
    # meaningful cost. The approved rule still resolves to the purchase cost.
    result = calculate_weighted_average_restock(
        old_quantity=Decimal("-10"),
        old_weighted_average=Decimal("2.00"),
        purchased_quantity=Decimal("5"),
        purchase_unit_cost=Decimal("3.00"),
    )
    assert result == Decimal("3.00")


def test_calculate_weighted_average_restock_rejects_non_positive_purchased_quantity():
    with pytest.raises(ValueError):
        calculate_weighted_average_restock(
            old_quantity=Decimal("10"),
            old_weighted_average=Decimal("1"),
            purchased_quantity=Decimal("0"),
            purchase_unit_cost=Decimal("1"),
        )
    with pytest.raises(ValueError):
        calculate_weighted_average_restock(
            old_quantity=Decimal("10"),
            old_weighted_average=Decimal("1"),
            purchased_quantity=Decimal("-5"),
            purchase_unit_cost=Decimal("1"),
        )


def test_calculate_weighted_average_restock_rejects_negative_purchase_unit_cost():
    with pytest.raises(ValueError):
        calculate_weighted_average_restock(
            old_quantity=Decimal("10"),
            old_weighted_average=Decimal("1"),
            purchased_quantity=Decimal("5"),
            purchase_unit_cost=Decimal("-1"),
        )


def test_calculate_weighted_average_restock_is_correct_under_a_tiny_ambient_context():
    with decimal.localcontext() as ambient:
        ambient.prec = 2
        result = calculate_weighted_average_restock(
            old_quantity=Decimal("123456789.123456"),
            old_weighted_average=Decimal("7.654321"),
            purchased_quantity=Decimal("987654.654321"),
            purchase_unit_cost=Decimal("3.210123"),
        )
    with decimal.localcontext() as reference_ctx:
        reference_ctx.prec = 50
        old_q = Decimal("123456789.123456")
        old_w = Decimal("7.654321")
        new_q = Decimal("987654.654321")
        new_c = Decimal("3.210123")
        reference = (old_q * old_w + new_q * new_c) / (old_q + new_q)
    assert result == reference


def test_calculate_weighted_average_restock_does_not_mutate_ambient_context():
    before = decimal.getcontext().prec
    calculate_weighted_average_restock(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))
    assert decimal.getcontext().prec == before


# --- calculate_total_cost -------------------------------------------------------------


def test_calculate_total_cost():
    assert calculate_total_cost(Decimal("2.500000"), Decimal("4.000000")) == Decimal("10.000000")


def test_calculate_total_cost_does_not_mutate_ambient_context():
    before = decimal.getcontext().prec
    calculate_total_cost(Decimal("1"), Decimal("1"))
    assert decimal.getcontext().prec == before


# --- resolve_effective_replacement_cost -----------------------------------------------


def test_effective_replacement_cost_prefers_explicit_override():
    assert resolve_effective_replacement_cost(Decimal("9.99"), Decimal("5.00")) == Decimal("9.99")


def test_effective_replacement_cost_falls_back_to_latest_when_no_override():
    assert resolve_effective_replacement_cost(None, Decimal("5.00")) == Decimal("5.00")


def test_effective_replacement_cost_is_none_when_both_unset():
    assert resolve_effective_replacement_cost(None, None) is None


# --- quantize_for_storage --------------------------------------------------------------


def test_quantize_for_storage_rounds_to_six_decimal_places():
    assert quantize_for_storage(Decimal("1.1234565")) == Decimal("1.123457")


def test_quantize_for_storage_uses_round_half_up_not_round_half_even():
    # 2.1234565 -> 7th digit is exactly 5, preceding stored digit is 6 (even).
    # ROUND_HALF_UP rounds away from zero regardless of parity: -> 2.123457.
    # ROUND_HALF_EVEN (Python Decimal's own ambient default) would instead round to
    # the nearest even digit and give 2.123456 — this test fails if the wrong mode is
    # ever substituted.
    result = quantize_for_storage(Decimal("2.1234565"))
    assert result == Decimal("2.123457")
    half_even_result = Decimal("2.1234565").quantize(
        Decimal("0.000001"), rounding=decimal.ROUND_HALF_EVEN
    )
    assert result != half_even_result


def test_quantize_for_storage_leaves_an_already_six_decimal_value_unchanged():
    assert quantize_for_storage(Decimal("12.345678")) == Decimal("12.345678")
    assert quantize_for_storage(Decimal("0")) == Decimal("0.000000")


def test_quantize_for_storage_does_not_mutate_ambient_context():
    before = decimal.getcontext().prec
    quantize_for_storage(Decimal("1.23456789"))
    assert decimal.getcontext().prec == before


# --- is_representable_in_numeric_18_6 (Phase 5 correction-pass finding 4) -------------


def test_max_magnitude_itself_is_representable():
    assert is_representable_in_numeric_18_6(NUMERIC_18_6_MAX_MAGNITUDE) is True
    assert is_representable_in_numeric_18_6(-NUMERIC_18_6_MAX_MAGNITUDE) is True


def test_one_unit_over_max_magnitude_is_not_representable():
    over = NUMERIC_18_6_MAX_MAGNITUDE + Decimal("0.000001")
    assert is_representable_in_numeric_18_6(over) is False
    assert is_representable_in_numeric_18_6(-over) is False


def test_ordinary_small_values_are_representable():
    assert is_representable_in_numeric_18_6(Decimal("0")) is True
    assert is_representable_in_numeric_18_6(Decimal("100.500000")) is True
    assert is_representable_in_numeric_18_6(Decimal("-100.500000")) is True
