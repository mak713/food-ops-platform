"""Unit tests for the deterministic UnitConversion domain module (Phase 4 Plan v4 §3a/§14)."""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest

from app.db.enums import MeasurementFamily
from app.domain.unit_conversion import (
    IncompatibleUnitFamilyError,
    UnknownUnitError,
    convert,
    unit_family,
)


def test_unit_family_reports_correct_family_for_every_supported_unit():
    weight = {"g", "kg", "oz", "lb"}
    volume = {"mL", "L", "tsp", "tbsp", "fl_oz", "cup"}
    count = {"each"}
    for unit in weight:
        assert unit_family(unit) is MeasurementFamily.WEIGHT
    for unit in volume:
        assert unit_family(unit) is MeasurementFamily.VOLUME
    for unit in count:
        assert unit_family(unit) is MeasurementFamily.COUNT


def test_unit_family_rejects_unknown_unit():
    with pytest.raises(UnknownUnitError):
        unit_family("gallon")


@pytest.mark.parametrize(
    ("quantity", "from_unit", "to_unit", "expected"),
    [
        (Decimal("16"), "oz", "g", Decimal("453.59237")),
        (Decimal("2"), "lb", "g", Decimal("907.18474")),
        (Decimal("1"), "kg", "g", Decimal("1000")),
        (Decimal("1"), "cup", "mL", Decimal("236.5882365")),
        (Decimal("2"), "tbsp", "mL", Decimal("29.5735295625")),
        (Decimal("2"), "tbsp", "fl_oz", Decimal("1")),  # 2 tbsp == 1 fl oz, cross-check
        (Decimal("1"), "L", "mL", Decimal("1000")),
        (Decimal("5"), "each", "each", Decimal("5")),
        (Decimal("100"), "g", "g", Decimal("100")),
    ],
)
def test_same_family_conversions(quantity, from_unit, to_unit, expected):
    assert convert(quantity, from_unit, to_unit) == expected


@pytest.mark.parametrize(
    ("from_unit", "to_unit"),
    [
        ("g", "mL"),
        ("mL", "g"),
        ("g", "each"),
        ("each", "g"),
        ("mL", "each"),
        ("each", "mL"),
        ("kg", "cup"),
        ("cup", "lb"),
    ],
)
def test_cross_family_conversion_rejected(from_unit, to_unit):
    with pytest.raises(IncompatibleUnitFamilyError):
        convert(Decimal("1"), from_unit, to_unit)


def test_convert_rejects_unknown_unit_on_either_side():
    with pytest.raises(UnknownUnitError):
        convert(Decimal("1"), "gallon", "g")
    with pytest.raises(UnknownUnitError):
        convert(Decimal("1"), "g", "gallon")


def test_convert_preserves_decimal_type_and_precision():
    result = convert(Decimal("28.349523125"), "oz", "g")
    assert isinstance(result, Decimal)
    assert result == Decimal("803.695461414909765625")


def test_convert_is_correct_even_under_a_tiny_ambient_decimal_context():
    quantity = Decimal("999999999999.999999")
    with decimal.localcontext() as ambient:
        ambient.prec = 2
        result = convert(quantity, "oz", "g")
    # Reference computed under an explicit, adequate precision — not the interpreter's
    # uncontrolled default context, which (prec=28) is itself too small for this exact
    # product's true digit count and would make a naive comparison flaky.
    with decimal.localcontext() as reference_ctx:
        reference_ctx.prec = 50
        reference = quantity * Decimal("28.349523125")
    assert result == reference


def test_convert_does_not_mutate_the_ambient_decimal_context():
    before = decimal.getcontext().prec
    convert(Decimal("1"), "oz", "g")
    assert decimal.getcontext().prec == before
