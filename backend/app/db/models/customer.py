from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business


class Customer(Base, TimestampMixin, VersionMixin):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customers_business_id_is_active", "business_id", "is_active"),)

    # Optimistic concurrency (Spec §8.33 explicitly names Customer; Phase 3 plan v3 §4):
    # applied per-class, not on VersionMixin itself, so User/Business (also VersionMixin
    # users) are unaffected — only the resources Phase 3 actually builds mutation
    # endpoints for get this behavior. `declared_attr.directive` is required rather than a
    # plain class-body dict because `version` is inherited from VersionMixin and isn't
    # resolvable as a bare name until the class's mapped attributes are fully configured.
    @declared_attr.directive
    def __mapper_args__(cls) -> dict:
        return {"version_id_col": cls.version}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    # Suggested values only (Spec §8.5: "Suggested preferred-contact values"), not a
    # DB-enforced closed set.
    preferred_contact_method: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    business: Mapped[Business] = relationship()
