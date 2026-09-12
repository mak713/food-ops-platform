"""Deterministic unit conversion (Spec §7.5/§15.1, Phase 4 Plan v4 §3a).

Pure, side-effect-free: no HTTP or database access (Spec §11.3). Conversion factors are
`Decimal` string literals only (never binary float) and all arithmetic runs inside an
explicit, fixed-precision `decimal.localcontext()` so results never depend on — or leak
into — whatever ambient `decimal.getcontext()` the caller happens to have set.

Canonical units (Spec §7.5, "should normally be"): WEIGHT -> g, VOLUME -> mL, COUNT -> each.
Cross-family conversion is unconditionally rejected (INV-003, REC-006, ADR-019) — the
application must never infer weight<->volume or count<->weight conversion.
"""

from __future__ import annotations

import decimal
from decimal import Decimal

from app.db.enums import MeasurementFamily

_CONTEXT_PRECISION = 50


class UnknownUnitError(ValueError):
    """Raised for a unit code outside the closed `UNIT_CODES` set."""


class IncompatibleUnitFamilyError(ValueError):
    """Raised when `from_unit` and `to_unit` belong to different measurement families."""


# unit code -> (family, factor to that family's canonical unit).
# WEIGHT factors are spec-given exactly (Spec §15.1): 1 kg = 1000 g; 1 oz = 28.349523125 g;
# 1 lb = 16 oz (=> 453.59237 g). VOLUME factors are the standard US-customary cooking
# constants (Phase 4 Plan v4 §3a — the spec mandates "one centralized mapping" without
# giving exact numbers; these are the approved values). COUNT has no sub-units.
_UNIT_FACTORS: dict[str, tuple[MeasurementFamily, Decimal]] = {
    "g": (MeasurementFamily.WEIGHT, Decimal("1")),
    "kg": (MeasurementFamily.WEIGHT, Decimal("1000")),
    "oz": (MeasurementFamily.WEIGHT, Decimal("28.349523125")),
    "lb": (MeasurementFamily.WEIGHT, Decimal("453.59237")),
    "mL": (MeasurementFamily.VOLUME, Decimal("1")),
    "L": (MeasurementFamily.VOLUME, Decimal("1000")),
    "tsp": (MeasurementFamily.VOLUME, Decimal("4.92892159375")),
    "tbsp": (MeasurementFamily.VOLUME, Decimal("14.78676478125")),
    "fl_oz": (MeasurementFamily.VOLUME, Decimal("29.5735295625")),
    "cup": (MeasurementFamily.VOLUME, Decimal("236.5882365")),
    "each": (MeasurementFamily.COUNT, Decimal("1")),
}


def unit_family(unit: str) -> MeasurementFamily:
    """Returns the `MeasurementFamily` a unit code belongs to. Raises `UnknownUnitError`
    for anything outside the closed unit set."""
    entry = _UNIT_FACTORS.get(unit)
    if entry is None:
        raise UnknownUnitError(f"Unknown unit code: {unit!r}")
    return entry[0]


def convert(quantity: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Converts `quantity` from `from_unit` to `to_unit`. Raises `UnknownUnitError` for
    an unrecognized unit, or `IncompatibleUnitFamilyError` if the two units belong to
    different measurement families — conversion is never inferred across families.

    Returns the full-precision result with no intermediate or final rounding; rounding a
    persisted value to its column's precision/scale is a service/schema-layer concern
    (Phase 4 Plan v4 §11), not this module's."""
    from_family, from_factor = _resolve(from_unit)
    to_family, to_factor = _resolve(to_unit)
    if from_family is not to_family:
        raise IncompatibleUnitFamilyError(
            f"Cannot convert {from_unit!r} ({from_family}) to {to_unit!r} ({to_family})"
        )
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        canonical = quantity * from_factor
        return canonical / to_factor


def _resolve(unit: str) -> tuple[MeasurementFamily, Decimal]:
    entry = _UNIT_FACTORS.get(unit)
    if entry is None:
        raise UnknownUnitError(f"Unknown unit code: {unit!r}")
    return entry
