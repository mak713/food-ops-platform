from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import QUANTITY
from app.db.enums import SurplusTransactionType, pg_enum
from app.db.mixins import CreatedAtMixin, TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.order import Order, OrderLine
    from app.db.models.product import Product
    from app.db.models.production import ProductionRequirement, ProductionRun


class SurplusInventory(Base, TimestampMixin, VersionMixin):
    __tablename__ = "surplus_inventory"
    __table_args__ = (
        CheckConstraint(
            "physical_quantity >= 0", name="ck_surplus_inventory_physical_quantity_non_negative"
        ),
        CheckConstraint(
            "unit_cost_basis >= 0", name="ck_surplus_inventory_unit_cost_basis_non_negative"
        ),
        Index(
            "ix_surplus_inventory_business_id_product_id_usable_through_date",
            "business_id",
            "product_id",
            "usable_through_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION"), nullable=False
    )
    source_production_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_runs.id", ondelete="SET NULL")
    )
    physical_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit_cost_basis: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    produced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usable_through_date: Mapped[date | None] = mapped_column(Date)
    is_reusable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship()
    source_production_run: Mapped[ProductionRun | None] = relationship()


class SurplusAllocation(Base, CreatedAtMixin):
    __tablename__ = "surplus_allocations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_surplus_allocations_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    surplus_inventory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("surplus_inventory.id", ondelete="NO ACTION"), nullable=False
    )
    production_requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_requirements.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_lines.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)

    business: Mapped[Business] = relationship()
    surplus_inventory: Mapped[SurplusInventory] = relationship()
    production_requirement: Mapped[ProductionRequirement] = relationship(
        back_populates="surplus_allocations"
    )
    order: Mapped[Order] = relationship()
    order_line: Mapped[OrderLine] = relationship()


class SurplusTransaction(Base, CreatedAtMixin):
    __tablename__ = "surplus_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    surplus_inventory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("surplus_inventory.id", ondelete="NO ACTION"), nullable=False
    )
    transaction_type: Mapped[SurplusTransactionType] = mapped_column(
        pg_enum(SurplusTransactionType, name="ck_surplus_transactions_transaction_type"),
        nullable=False,
    )
    # No CHECK: signed by design.
    quantity_change: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_lines.id", ondelete="SET NULL")
    )
    production_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_runs.id", ondelete="SET NULL")
    )

    business: Mapped[Business] = relationship()
    surplus_inventory: Mapped[SurplusInventory] = relationship()


class PurchasedProductReservation(Base, TimestampMixin):
    __tablename__ = "purchased_product_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchased_product_reservations_quantity_positive"),
        Index(
            "ix_purchased_product_reservations_business_id_product_id",
            "business_id",
            "product_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_lines.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship()
    order: Mapped[Order] = relationship()
    order_line: Mapped[OrderLine] = relationship()
