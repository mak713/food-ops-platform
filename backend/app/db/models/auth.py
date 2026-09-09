from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import CreatedAtMixin

# Framework-infrastructure tables (Spec §8.1, ADR-072) — not Spec §8 domain entities, so
# their column shapes are a Phase 2 implementation decision rather than a spec-mandated
# list. Kept in their own module, separate from app/db/models/*.py domain models, per the
# spec's requirement that these stay "clearly separated from the business domain."
_TIMESTAMPTZ = DateTime(timezone=True)


class AuthSession(Base, CreatedAtMixin):
    """A server-side opaque session record backing the `fo_session`/`fo_csrf` cookie pair.

    Named `AuthSession`, not `Session`, purely to avoid colliding with
    `sqlalchemy.orm.Session`, which is imported throughout this codebase — the table itself
    is `sessions`. Only `token_hash`/`csrf_token_hash` (SHA-256 of the opaque raw values
    handed to the browser) are ever persisted; the raw tokens themselves never reach the
    database. `expires_at` is the current sliding-idle deadline (recomputed on each
    authenticated request as `min(now + 7d, created_at + 30d)`) — no separate absolute-
    expiry column is needed since the cap is computed from `created_at`.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ)


class PasswordResetToken(Base, CreatedAtMixin):
    """A single-use, expiring password-reset token record.

    Only `token_hash` (SHA-256 of the opaque raw token emailed to the user) is persisted —
    the raw token is never stored, logged, or returned by any API response. `used_at` makes
    the row single-use; reset confirmation consumes it inside a row-locked transaction
    (`SELECT ... FOR UPDATE`) so two concurrent confirmations of the same token can't both
    succeed.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ)
