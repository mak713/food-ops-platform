"""Derived availability calculators (Spec §7.3, INV-004/INV-005, SUR-003; Phase 9).

Reserved/allocated and available quantities are *derived*, never stored master fields:

    ingredient_available = ingredient_physical - active_ingredient_reservations
    surplus_available    = surplus_physical    - active_surplus_allocations

Pure, side-effect-free (ADR-109): Decimal-only arithmetic inside its own explicit
`decimal.localcontext()`. An Ingredient's available quantity may legitimately be
negative (reservations exceeding physical stock, or a negative physical balance); it is
reported as computed, never clamped.
"""

from __future__ import annotations

import decimal
from decimal import Decimal

_CONTEXT_PRECISION = 50


def calculate_available(physical_quantity: Decimal, committed_quantity: Decimal) -> Decimal:
    """`physical - committed` (reserved for an Ingredient, allocated for a Surplus lot)."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return physical_quantity - committed_quantity
