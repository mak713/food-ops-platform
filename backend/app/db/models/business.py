from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import MONEY, RATIO
from app.db.enums import FulfillmentMethod, pg_enum
from app.db.mixins import TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class Business(Base, TimestampMixin, VersionMixin):
    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint(
            "default_labor_rate >= 0", name="ck_businesses_default_labor_rate_non_negative"
        ),
        CheckConstraint(
            "target_contribution_margin IS NULL OR "
            "(target_contribution_margin >= 0 AND target_contribution_margin < 1)",
            name="ck_businesses_target_contribution_margin_range",
        ),
        CheckConstraint(
            "fulfillment_warning_minutes > 0",
            name="ck_businesses_fulfillment_warning_minutes_positive",
        ),
        CheckConstraint(
            "shopping_horizon_days IN (3, 7)",
            name="ck_businesses_shopping_horizon_days_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="NO ACTION"),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    business_type: Mapped[str | None] = mapped_column(String)
    contact_phone: Mapped[str | None] = mapped_column(String)
    contact_email: Mapped[str | None] = mapped_column(String)
    address_text: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    default_fulfillment_method: Mapped[FulfillmentMethod | None] = mapped_column(
        pg_enum(FulfillmentMethod, name="ck_businesses_default_fulfillment_method")
    )
    default_labor_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    target_contribution_margin: Mapped[Decimal | None] = mapped_column(RATIO)
    fulfillment_warning_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    shopping_horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)

    owner: Mapped[User] = relationship(back_populates="business")
