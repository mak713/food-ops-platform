"""Phase 7 derived-value representability guards (Final Architecture Lock §G).

Pure primitives only — no HTTP/database access, no `ApiError` import (ADR-109's "no
HTTP or database access" bar, extended here to mean no application-error types
either). Callers at the service layer translate a `False`/rejected result into a
structured `PRODUCTION_REQUIREMENT_VALUE_OVERFLOW` `ApiError` and own transaction
rollback; this module never raises anything itself.

`NUMERIC(18,6)` quantization/representability is deliberately NOT duplicated here —
callers reuse `app.domain.inventory_costing.quantize_for_storage`/
`is_representable_in_numeric_18_6` directly, which already implement the Phase 5
convention (`Decimal("0.000001")`, `ROUND_HALF_UP`) Phase 7 follows as-is for its own
`NUMERIC(18,6)` projection/cost columns.
"""

from __future__ import annotations

#: PostgreSQL's signed 32-bit INTEGER range. `recommended_batches`,
#: `estimated_active_minutes`, and `estimated_elapsed_minutes` are each additionally
#: constrained non-negative by their own DB CHECK constraint, so 0 is the practical
#: lower bound Phase 7 ever produces — the full signed range is validated here for a
#: general-purpose guard, matching the `le=2_147_483_647` precedent already
#: established for `OrderLineInput.custom_active_time_minutes`.
INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647


def is_representable_in_int32(value: int) -> bool:
    """True if `value` fits PostgreSQL's signed 32-bit INTEGER column shape. A tiny
    Recipe yield combined with a very large remaining demand can legitimately drive a
    derived `recommended_batches`/minutes count past this bound."""
    return INT32_MIN <= value <= INT32_MAX
