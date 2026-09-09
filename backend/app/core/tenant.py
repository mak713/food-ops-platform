"""The non-disclosing tenant-lookup helper (Spec §10.5/17.12).

`NotFoundError` is raised identically whether a resource truly doesn't exist or exists but
belongs to a different Business — callers must never distinguish the two, so a foreign-
tenant reference behaves exactly like a missing one. Phase 2 has no client-facing
tenant-owned resource reachable by ID yet (`Business` is only ever resolved via the
session), so this establishes the reusable pattern Phase 3+ tenant-scoped services/routers
will raise into, rather than exercising it end-to-end through a real endpoint here
(Phase 2 plan §13).
"""

from __future__ import annotations

from app.core.api_errors import ApiError

_NOT_FOUND_MESSAGE = "The requested resource was not found."


class NotFoundError(ApiError):
    def __init__(self) -> None:
        super().__init__(404, "NOT_FOUND", _NOT_FOUND_MESSAGE)
