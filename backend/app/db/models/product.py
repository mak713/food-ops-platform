from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import MONEY, QUANTITY
from app.db.enums import ProductType, pg_enum
from app.db.mixins import TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.recipe import Recipe


class Product(Base, TimestampMixin, VersionMixin):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "default_packaging_cost >= 0", name="ck_products_default_packaging_cost_non_negative"
        ),
        CheckConstraint(
            "default_surplus_usable_days IS NULL OR default_surplus_usable_days > 0",
            name="ck_products_default_surplus_usable_days_positive",
        ),
        Index(
            "ix_products_business_id_product_type_is_active",
            "business_id",
            "product_type",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    product_type: Mapped[ProductType] = mapped_column(
        pg_enum(ProductType, name="ck_products_product_type"), nullable=False
    )
    default_packaging_cost: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="0"
    )
    can_reuse_surplus: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    default_surplus_usable_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    business: Mapped[Business] = relationship()
    selling_options: Mapped[list[SellingOption]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )
    recipe: Mapped[Recipe | None] = relationship(
        back_populates="product",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SellingOption(Base, TimestampMixin, VersionMixin):
    __tablename__ = "selling_options"
    __table_args__ = (
        CheckConstraint("quantity_units > 0", name="ck_selling_options_quantity_units_positive"),
        CheckConstraint("price >= 0", name="ck_selling_options_price_non_negative"),
        CheckConstraint(
            "packaging_cost >= 0", name="ck_selling_options_packaging_cost_non_negative"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    quantity_units: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    packaging_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship(back_populates="selling_options")
