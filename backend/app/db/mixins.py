from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

# business_id is intentionally NOT a mixin: tenant ownership is the single most
# security-critical column in this schema and must be visibly declared on every
# tenant-owned model's own class body, not hidden behind shared inheritance.

# Mapped[datetime] alone does not imply a timezone-aware column — SQLAlchemy's
# default type-map for `datetime` renders plain DateTime (no tz). Spec §8.2
# requires TIMESTAMPTZ everywhere, so the column type is given explicitly here.
_TIMESTAMPTZ = DateTime(timezone=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        _TIMESTAMPTZ, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        _TIMESTAMPTZ, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CreatedAtMixin:
    """For schema rows that have `created_at` but no `updated_at` column.

    This describes the column shape only — it is not a claim that every row
    using it is immutable. Some created-at-only rows are legitimately updated
    in place during their lifecycle (e.g. `RecipeRevision.is_current` flips as
    revisions change; `OrderLine` fields change during allowed order-editing
    states) without gaining an `updated_at` column, because the spec's own
    column list for that table (Spec §8) doesn't include one.
    """

    created_at: Mapped[datetime] = mapped_column(
        _TIMESTAMPTZ, server_default=func.now(), nullable=False
    )


class VersionMixin:
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")
