"""Deterministic inventory-costing calculators (Spec §15.3/§15.4, Phase 5 Plan §C/§J).

Pure, side-effect-free: no HTTP or database access (Spec §11.3), matching the
`unit_conversion.py`/`recipe_scaling.py` precedent exactly. All Decimal arithmetic runs
inside an explicit, fixed-precision `decimal.localcontext()` so results never depend on —
or leak into — whatever ambient `decimal.getcontext()` the caller has set, and every public
function here returns full, unrounded precision. Rounding a persisted value to its
`NUMERIC(18,6)` column's precision/scale is done exactly once, via `quantize_for_storage`,
at the service layer immediately before assignment/insert — never inside these calculators,
and never more than once per value (Phase 5 Plan §J-1 / approval decision 7).
"""

from __future__ import annotations

import decimal
from decimal import Decimal

_CONTEXT_PRECISION = 50

#: The single Phase 5 storage-quantization rule: six decimal places (matching every
#: Phase 5 `NUMERIC(18,6)` column), rounded half-away-from-zero. This exact `Decimal`/
#: `rounding` pair is the only place either value is chosen — every service that persists
#: a computed inventory Decimal must go through `quantize_for_storage` rather than calling
#: `.quantize(...)` directly, so the storage rule stays defined and testable in one place.
_STORAGE_QUANTUM = Decimal("0.000001")
_STORAGE_ROUNDING = decimal.ROUND_HALF_UP

#: `NUMERIC(18,6)` stores at most 18 total significant digits, 6 of them fractional — so
#: at most 12 integer digits. This is the largest magnitude the frozen schema can hold in
#: any Phase 5 quantity/cost column, independent of sign (several of these columns, e.g.
#: `ingredients.physical_quantity`/`inventory_transactions.quantity_change`, are signed).
NUMERIC_18_6_MAX_MAGNITUDE = Decimal("999999999999.999999")


def quantize_for_storage(value: Decimal) -> Decimal:
    """Rounds a computed Decimal to the Phase 5 storage rule (6 decimal places,
    `ROUND_HALF_UP`) before it is persisted into a `NUMERIC(18,6)` column or transaction
    field. This is the sole quantization point for every Phase 5 inventory service —
    domain calculators themselves (`calculate_weighted_average_restock`,
    `unit_conversion.convert`/`convert_unit_cost`) always return unrounded full precision;
    only this function ever rounds a value that is about to be stored."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return value.quantize(_STORAGE_QUANTUM, rounding=_STORAGE_ROUNDING)


def calculate_weighted_average_restock(
    old_quantity: Decimal,
    old_weighted_average: Decimal,
    purchased_quantity: Decimal,
    purchase_unit_cost: Decimal,
) -> Decimal:
    """Implements Spec §15.3's weighted-average restock formula for a normal positive
    pre-restock balance, plus the approved non-positive-balance rule (Phase 5 Plan §J-1):

    - `old_quantity > 0`:
      `(old_quantity * old_weighted_average + purchased_quantity * purchase_unit_cost)
       / (old_quantity + purchased_quantity)`
    - `old_quantity <= 0`: the pre-restock balance is a deficit/reconciliation state, not
      positively-valued stock, and therefore carries no quantity weight into the new
      blend — `new_weighted_average = purchase_unit_cost` outright, discarding
      `old_quantity`/`old_weighted_average` from the calculation entirely rather than
      blending a negative or zero weight into it.

    All arguments are expected to already be normalized to the resource's canonical unit
    (Ingredient) or already in the Product's own units (Purchased Product Inventory), and
    are used here at whatever precision the caller passes in — the caller (the service
    layer) is responsible for quantizing to 6 decimal places both before calling this (for
    `purchased_quantity`/`purchase_unit_cost`, which are themselves about to be persisted
    as `quantity_change`/`unit_cost`) and once more after calling this (for the returned
    weighted average itself), per `quantize_for_storage`. Raises `ValueError` for a
    non-positive `purchased_quantity` or a negative `purchase_unit_cost` — a restock always
    adds a positive quantity at a valid cost."""
    if purchased_quantity <= 0:
        raise ValueError(f"purchased_quantity must be > 0, got {purchased_quantity}")
    if purchase_unit_cost < 0:
        raise ValueError(f"purchase_unit_cost must be >= 0, got {purchase_unit_cost}")

    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        if old_quantity <= 0:
            return purchase_unit_cost
        return (old_quantity * old_weighted_average + purchased_quantity * purchase_unit_cost) / (
            old_quantity + purchased_quantity
        )


def calculate_total_cost(quantity: Decimal, unit_cost: Decimal) -> Decimal:
    """Pure `quantity * unit_cost` multiply, isolated in its own context for consistency
    with ADR-109 even though trivial. Per the approved ledger-auditability ordering (Phase
    5 Plan §J-1 / approval decision 7), the service layer always passes already-quantized
    (6-decimal-place) `quantity`/`unit_cost` here — the ones actually persisted on the same
    transaction row — and quantizes the result once more, so a stored `total_cost` is
    always exactly the arithmetic product of that same row's stored `quantity_change` and
    `unit_cost`."""
    with decimal.localcontext() as ctx:
        ctx.prec = _CONTEXT_PRECISION
        return quantity * unit_cost


def resolve_effective_replacement_cost(
    replacement_override: Decimal | None, latest_purchase_cost: Decimal | None
) -> Decimal | None:
    """The single authoritative implementation of Spec §15.4's effective-replacement-cost
    rule (Phase 5 Plan approval decision 5): an explicit, seller-maintained
    `replacement_override` (non-`NULL`) always wins; otherwise the effective replacement
    cost falls back to `latest_purchase_cost` (itself possibly `None`, meaning unknown/
    unset — nothing has ever been purchased or explicitly overridden). Every response
    mapper and any future planned-cost consumer must call this rather than independently
    reimplementing the `override ?? latest` fallback."""
    if replacement_override is not None:
        return replacement_override
    return latest_purchase_cost


def is_representable_in_numeric_18_6(value: Decimal) -> bool:
    """True if `value` fits within the frozen `NUMERIC(18,6)` column shape (Phase 5
    correction-pass finding 4). Callers should pass an already-`quantize_for_storage`d
    value — this checks magnitude only, not scale, since every value reaching here has
    already been rounded to exactly 6 decimal places.

    A raw request value can never itself violate this (Pydantic's own
    `max_digits=18, decimal_places=6` on every Phase 5 request field already guarantees a
    12-digit integer-part ceiling), but a *derived* value can: converting a
    maximal-but-individually-valid quantity into a canonical unit with a large conversion
    factor (e.g. kg -> g multiplies by 1000), adding two individually-valid balances
    together (a restock/adjustment against an already-large `physical_quantity`), or
    multiplying two individually-valid Decimals together (`quantity * unit_cost` for
    `total_cost`) can all produce a result exceeding what the column can actually store.
    This function is pure and side-effect-free (ADR-109) — it raises nothing; the calling
    service decides how to translate a `False` result into an HTTP error."""
    return abs(value) <= NUMERIC_18_6_MAX_MAGNITUDE
