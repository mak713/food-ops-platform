"""The non-disclosing tenant-lookup helper (Spec §10.5/17.12), plus the scoped-lookup and
optimistic-concurrency helpers Phase 3 is the first phase to actually use.

`NotFoundError` is raised identically whether a resource truly doesn't exist or exists but
belongs to a different Business — callers must never distinguish the two, so a foreign-
tenant reference behaves exactly like a missing one. Phase 2 had no client-facing
tenant-owned resource reachable by ID yet (`Business` is only ever resolved via the
session), so it established the reusable pattern; Phase 3 (plan v3 §3a/§4) applies it via
`get_owned_or_404`/`get_selling_option_for_product`/`check_version` below.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.product import Product, SellingOption

_NOT_FOUND_MESSAGE = "The requested resource was not found."
_STALE_VERSION_MESSAGE = (
    "This record changed since you opened it. Refresh the latest version and review your "
    "changes before saving again."
)


class NotFoundError(ApiError):
    def __init__(self) -> None:
        super().__init__(404, "NOT_FOUND", _NOT_FOUND_MESSAGE)


def get_owned_or_404[T](db: Session, model: type[T], id_: uuid.UUID, business: Business) -> T:
    """Fetch a row scoped by (id, business_id) in one query (Phase 3 plan v3 §3a).

    Missing and foreign-tenant rows are indistinguishable — both produce zero rows from
    this SELECT, so both raise NotFoundError identically (Spec §10.5). There is no code
    path where a row belonging to another business is ever loaded into memory.
    """
    obj = db.scalar(select(model).where(model.id == id_, model.business_id == business.id))
    if obj is None:
        raise NotFoundError()
    return obj


def get_selling_option_for_product(
    db: Session, product: Product, option_id: uuid.UUID
) -> SellingOption:
    """`product` must already be the caller's own tenant-scoped Product (fetched via
    get_owned_or_404 first). Scopes the SELECT by (id, product_id, business_id) together,
    so a SellingOption belonging to a different product OR a different tenant is equally
    unreachable in one query (Phase 3 plan v3 §3a). The `business_id` predicate is
    technically redundant once `product` is tenant-verified and `product_id` matches —
    included anyway as defense-in-depth (Spec §3.4).
    """
    obj = db.scalar(
        select(SellingOption).where(
            SellingOption.id == option_id,
            SellingOption.product_id == product.id,
            SellingOption.business_id == product.business_id,
        )
    )
    if obj is None:
        raise NotFoundError()
    return obj


def stale_version_error(*, resource: str, current_version: int | None = None) -> ApiError:
    return ApiError(
        409,
        "STALE_VERSION",
        _STALE_VERSION_MESSAGE,
        issues=[
            {
                "severity": "ERROR",
                "code": "STALE_VERSION",
                "message": _STALE_VERSION_MESSAGE,
                "field": "version",
                "resource": resource,
                "details": {"current_version": current_version},
            }
        ],
    )


def check_version(obj: object, expected_version: int, *, resource: str) -> None:
    """Raise 409 STALE_VERSION if `expected_version` (submitted by the client with its
    mutation request) doesn't match the currently-loaded row's version (Spec §8.33/§17.8).
    Call this after fetching via get_owned_or_404 but before mutating anything — including
    before an idempotent no-op check (Phase 3 plan v3 §7), since the client's view of the
    record must be current regardless of what the end result would be. This is the
    application-level pre-check for the common cross-request case; `commit_or_raise_stale`
    below is the ORM-level backstop for a true within-transaction race.
    """
    current_version = obj.version  # type: ignore[attr-defined]
    if current_version != expected_version:
        raise stale_version_error(resource=resource, current_version=current_version)


def commit_or_raise_stale(db: Session, *, resource: str) -> None:
    """Commit, translating a `StaleDataError` (raised by SQLAlchemy's `version_id_col`
    mechanism — Spec §8.2 "where the ORM implementation remains clean" — when a genuine
    concurrent-transaction race caused the UPDATE's WHERE clause to match zero rows) into
    the same 409 STALE_VERSION envelope the application-level `check_version` pre-check
    produces (Phase 3 plan v3 §4/§18). This is the defense-in-depth backstop for the
    narrow window `check_version` can't catch — the common case is still `check_version`.
    """
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise stale_version_error(resource=resource) from None
