"""Exact integer apportionment of a completed Production Run's actual total cost
across its direct Order-line output allocations and any resulting Surplus excess
(RUN-009; Phase 8 Final Architecture Amendment §2 / Final Architecture Lock Addendum
§B).

Pure, side-effect-free (ADR-109). Deliberately avoids proving cost preservation via
Decimal division — a quotient like `100.00 / 3` never terminates at any finite
precision, so "compute one unrounded per-unit cost, multiply back out" cannot be
exact for every input. This module instead uses the Hamilton / largest-remainder
apportionment method on exact integers, which is exact by construction for any input,
including non-terminating ratios.

**Two independent precision boundaries, handled as two independent passes** — a
correction discovered during implementation (Final Architecture Lock's/Amendment's
"C_cents" formula assumed `ProductionRun.actual_total_production_cost` was `MONEY`
(`NUMERIC(14,2)`); direct inspection of the schema shows it is actually `QUANTITY`
(`NUMERIC(18,6)`), matching the column-type rule that internal, non-customer-facing
cost aggregates use `QUANTITY` precision even though `OrderCostAllocation.amount` and
`SurplusInventory.source_total_cost_basis` are `MONEY`):

1. **Micro-unit pass** — apportion the run's exact 6-decimal-place total (in
   micro-units, `x 1_000_000`) across every recipient (each direct
   `ProductionRunOrderAllocation` plus, if `surplus_excess_quantity > 0`, one Surplus
   recipient) in proportion to each recipient's quantity. Exact: `Σ final_micro_units
   == C_micros`.
2. **Cents pass** — reduce *every* recipient's micro-unit share (direct Order lines
   **and**, when present, the single Surplus recipient) to whole cents (`MONEY`,
   `NUMERIC(14,2)`) via a **second Hamilton pass across all of them together**,
   targeting the correctly-rounded cents value of the *combined* `C_micros` total
   computed once. Rounding the direct group and the Surplus recipient independently
   (two separate roundings) does not in general reconstruct the same total as
   rounding the whole exactly once — e.g. two halves each rounding up from 0.5 cents
   overstates the combined total by a full cent versus rounding their exact 1-cent
   sum directly — so this module always apportions cents in one combined pass.

`unit_cost_basis` (the ordinary, already-established 6-decimal per-unit operational
figure) is unaffected by and unrelated to this module — it remains a separate,
already-accepted approximation, not claimed to itself reconstruct
`source_total_cost_basis` when multiplied by a *current* (possibly since-reduced)
physical quantity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_MICRO_UNITS_PER_UNIT = 1_000_000
_MICRO_UNITS_PER_CENT = 10_000  # 1_000_000 micro-units per dollar / 100 cents per dollar
_MONEY_QUANTUM = Decimal("0.01")


def _to_exact_int(value: Decimal, *, scale: int) -> int:
    """Converts an already-quantized Decimal to an exact integer count of
    `10 ** -scale` units. `ROUND_HALF_UP` is defensive only — every value this module
    receives is already exactly representable at `scale` decimal places (it was
    quantized to that precision before persistence), so this never actually rounds in
    practice; it exists so a latent upstream precision bug fails loudly as a wrong
    total (caught by the exactness assertions below) rather than silently truncating."""
    return int((value * (Decimal(10) ** scale)).to_integral_value(rounding=ROUND_HALF_UP))


def _hamilton_apportion(total: int, weights: list[int]) -> list[int]:
    """Largest-remainder (Hamilton) apportionment: distributes the exact integer
    `total` across recipients in proportion to `weights` (parallel list, exact
    integers, `sum(weights) > 0`). Deterministic: ties in remainder go to the
    recipient appearing earlier in `weights` — callers pass weights pre-sorted in
    their own established priority order (earliest-fulfillment-deadline first for
    Order recipients; the Surplus recipient always passed last, so it loses every
    tie). `sum(result) == total` exactly, always."""
    weight_sum = sum(weights)
    bases: list[int] = []
    remainders: list[int] = []
    for weight in weights:
        numerator = total * weight
        bases.append(numerator // weight_sum)
        remainders.append(numerator % weight_sum)

    remaining = total - sum(bases)
    # Stable sort by remainder descending; index (== established priority order)
    # is the deterministic tie-break, since Python's sort is stable and we sort by
    # (-remainder) only.
    bonus_order = sorted(range(len(weights)), key=lambda i: -remainders[i])
    bonus_recipients = set(bonus_order[:remaining])

    return [base + (1 if i in bonus_recipients else 0) for i, base in enumerate(bases)]


@dataclass(frozen=True)
class CostRecipient:
    """One direct recipient of this run's output — always an OrderLine, in the same
    priority order `production_output_allocation.allocate_production_output` already
    produced (earliest-fulfillment-deadline first)."""

    order_line_id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True)
class CostApportionmentResult:
    order_line_amounts: dict[uuid.UUID, Decimal]  # order_line_id -> MONEY-precision amount
    surplus_source_total_cost_basis: Decimal | None  # MONEY-precision; None iff no excess


def apportion_production_cost(
    actual_total_production_cost: Decimal,
    actual_output_quantity: Decimal,
    direct_recipients: list[CostRecipient],
    surplus_excess_quantity: Decimal,
) -> CostApportionmentResult:
    """`actual_total_production_cost` and `actual_output_quantity` must already be at
    their stored `QUANTITY` (6dp) precision. `direct_recipients` must already be in
    the caller's established priority order and their quantities, plus
    `surplus_excess_quantity`, must sum exactly to `actual_output_quantity` (true by
    construction when built from `production_output_allocation`'s own output).
    Raises `ValueError` if `actual_output_quantity <= 0` — the zero-output case is
    never apportioned at all (Phase 8 Final Architecture Amendment §1); callers must
    not invoke this module for it."""
    if actual_output_quantity <= 0:
        raise ValueError("apportion_production_cost requires a positive actual_output_quantity")

    c_micros = _to_exact_int(actual_total_production_cost, scale=6)
    q_micros = _to_exact_int(actual_output_quantity, scale=6)

    has_surplus = surplus_excess_quantity > 0
    weights = [_to_exact_int(r.quantity, scale=6) for r in direct_recipients]
    if has_surplus:
        weights.append(_to_exact_int(surplus_excess_quantity, scale=6))

    if not weights or sum(weights) == 0:
        # No recipients at all (shouldn't happen when actual_output_quantity > 0
        # and quantities sum to it, but guarded rather than dividing by zero).
        return CostApportionmentResult(order_line_amounts={}, surplus_source_total_cost_basis=None)

    if sum(weights) != q_micros:
        # Caller contract violation (Phase 8 Final Architecture Lock §7): recipient
        # quantities plus surplus excess must sum exactly to actual_output_quantity.
        # Failing loudly here is far safer than silently apportioning against a
        # mismatched denominator.
        raise ValueError(
            "direct_recipients quantities plus surplus_excess_quantity must sum "
            "exactly to actual_output_quantity"
        )

    micro_shares = _hamilton_apportion(c_micros, weights)
    assert sum(micro_shares) == c_micros  # exactness guarantee — see module docstring

    # Cents pass: reduce EVERY recipient's micro-unit share (direct + Surplus
    # together, in one combined Hamilton apportionment) to whole cents, targeting
    # the single correctly-rounded (round-half-up) cents value of the combined
    # C_micros total. This is what guarantees the exact invariant holds across the
    # direct/Surplus boundary, not just within each side independently.
    target_cents = (c_micros + _MICRO_UNITS_PER_CENT // 2) // _MICRO_UNITS_PER_CENT
    cents_shares = (
        _hamilton_apportion(target_cents, micro_shares)
        if sum(micro_shares) > 0
        else [0] * len(micro_shares)
    )
    assert sum(cents_shares) == target_cents  # exactness guarantee — see module docstring

    order_line_amounts: dict[uuid.UUID, Decimal] = {
        recipient.order_line_id: (Decimal(cents) * _MONEY_QUANTUM).quantize(_MONEY_QUANTUM)
        for recipient, cents in zip(direct_recipients, cents_shares, strict=False)
    }
    surplus_source_total_cost_basis: Decimal | None = None
    if has_surplus:
        surplus_source_total_cost_basis = (Decimal(cents_shares[-1]) * _MONEY_QUANTUM).quantize(
            _MONEY_QUANTUM
        )

    return CostApportionmentResult(
        order_line_amounts=order_line_amounts,
        surplus_source_total_cost_basis=surplus_source_total_cost_basis,
    )
