"""Shared response envelope for paginated list endpoints (Phase 3 plan v3 §3).

The first schema module to need this — Phase 2's endpoints are all single-resource.
`PageResponse[T]` is deliberately generic rather than a per-resource duplicate, since every
Phase 3+ list endpoint uses the identical `items`/`total`/`limit`/`offset` shape.
"""

from __future__ import annotations

from pydantic import BaseModel


class PageResponse[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
