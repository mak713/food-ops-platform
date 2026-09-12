"""Deterministic whole-batch/recipe-scaling calculator foundation (Spec §22 Phase 4;
Phase 4 Plan v4 §3b).

Pure, side-effect-free: no HTTP or database access (Spec §11.3), no persistence, no demand
aggregation, no orchestration. Phase 7's Operational Recalculation Engine is the intended
composer of this module: it will compute "remaining demand" from real confirmed-order data
(entirely out of Phase 4 scope) and call `calculate_whole_batch_plan`/
`scale_ingredient_quantity` with that number plus a Recipe Revision's own `yield_quantity`/
ingredient-line quantities to get deterministic, testable batch/output/excess/scaled-line
results, which it alone persists into `production_requirements`/`production_runs`. Phase 4
does not itself call this module from any Recipe/Ingredient service — it exists now only as
the approved pure foundation Spec §22 requires ("deterministic whole-batch/recipe scaling
calculator foundation"), not wired into Recipe CRUD/revision workflows this phase.

All Decimal arithmetic in both public functions below runs inside its own explicit,
fixed-precision `decimal.localcontext()` — never dependent on, or leaking into, whatever
ambient `decimal.getcontext()` the caller has set.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal

_CONTEXT_PRECISION = 50


@dataclass(frozen=True)
class WholeBatchPlan:
    """The three results of a whole-batch scaling calculation, always derived together
    from one consistent `(remaining_demand, recipe_yield)` pair — never independently
    constructible, so a caller cannot pair a `required_batches` from one calculation with
    an `expected_excess` computed against unrelated inputs."""

    required_batches: int
    expected_output: Decimal
    expected_excess: Decimal


def calculate_whole_batch_plan(remaining_demand: Decimal, recipe_yield: Decimal) -> WholeBatchPlan:
    """`required_batches` is the smallest non-negative whole batch count whose output
    meets or exceeds `remaining_demand` (Decimal-safe ceiling of `remaining_demand /
    recipe_yield`, via `ROUND_CEILING` — never an integer-arithmetic floor-division trick,
    which truncates toward zero for `Decimal` and silently undercounts, e.g. 7/3 would
    wrongly yield 2 instead of the correct 3). `expected_output = required_batches *
    recipe_yield`; `expected_excess = expected_output - remaining_demand`, always >= 0 by
    construction. `remaining_demand == 0` short-circuits to 0/0/0.

    Raises `ValueError` for `remaining_demand < 0` or `recipe_yield <= 0` — this module
    re-validates defensively rather than trusting an already-validated caller, since it is
    a pure, reusable, DB-independent utility later phases may call directly.
    """
    if remaining_demand < 0:
        raise ValueError(f"remaining_demand must be >= 0, got {remaining_demand}")
    if recipe_yield <= 0:
        raise ValueError(f"recipe_yield must be > 0, got {recipe_yield}")

    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        if remaining_demand == 0:
            return WholeBatchPlan(0, Decimal(0), Decimal(0))
        quotient = (remaining_demand / recipe_yield).to_integral_value(
            rounding=decimal.ROUND_CEILING
        )
        batches = int(quotient)
        expected_output = Decimal(batches) * recipe_yield
        expected_excess = expected_output - remaining_demand
        return WholeBatchPlan(batches, expected_output, expected_excess)


def scale_ingredient_quantity(per_batch_quantity: Decimal, batches: int) -> Decimal:
    """Scales one recipe-revision-ingredient line's per-batch quantity by a whole batch
    count. Raises `ValueError` for a non-positive `per_batch_quantity` (matching the DB
    CHECK already on `recipe_revision_ingredients.quantity`) or a `batches` that isn't a
    non-negative `int` — never silently coerced."""
    if per_batch_quantity <= 0:
        raise ValueError(f"per_batch_quantity must be > 0, got {per_batch_quantity}")
    if not isinstance(batches, int) or isinstance(batches, bool) or batches < 0:
        raise ValueError(f"batches must be a non-negative int, got {batches!r}")

    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return per_batch_quantity * batches
