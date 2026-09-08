from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import MONEY, QUANTITY, RATIO
from app.db.enums import FulfillmentMethod, OrderLineType, OrderStatus, pg_enum
from app.db.mixins import CreatedAtMixin, TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.customer import Customer
    from app.db.models.product import Product, SellingOption


class Order(Base, TimestampMixin, VersionMixin):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="ck_orders_subtotal_non_negative"),
        CheckConstraint("manual_tax >= 0", name="ck_orders_manual_tax_non_negative"),
        CheckConstraint("final_total >= 0", name="ck_orders_final_total_non_negative"),
        CheckConstraint(
            "order_adjustment = 0 OR adjustment_description IS NOT NULL",
            name="ck_orders_adjustment_description_required",
        ),
        CheckConstraint(
            "estimated_direct_cost IS NULL OR estimated_direct_cost >= 0",
            name="ck_orders_estimated_direct_cost_non_negative",
        ),
        UniqueConstraint("business_id", "order_number", name="uq_orders_business_id_order_number"),
        Index("ix_orders_business_id_status", "business_id", "status"),
        Index("ix_orders_business_id_fulfillment_date", "business_id", "fulfillment_date"),
        Index("ix_orders_business_id_completed_at", "business_id", "completed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    order_number: Mapped[str] = mapped_column(String, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="NO ACTION")
    )
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, name="ck_orders_status"), nullable=False
    )
    fulfillment_date: Mapped[date | None] = mapped_column(Date)
    fulfillment_time: Mapped[time | None] = mapped_column(Time)
    fulfillment_method: Mapped[FulfillmentMethod | None] = mapped_column(
        pg_enum(FulfillmentMethod, name="ck_orders_fulfillment_method")
    )
    fulfillment_details: Mapped[str | None] = mapped_column(Text)
    fulfillment_notes: Mapped[str | None] = mapped_column(Text)
    internal_notes: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    # No CHECK: explicitly signed ("positive or negative", ORD-012).
    order_adjustment: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    manual_tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    final_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    adjustment_description: Mapped[str | None] = mapped_column(String)
    estimated_direct_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    # No CHECK: a loss-making order (contribution < 0) is a real scenario.
    estimated_contribution: Mapped[Decimal | None] = mapped_column(MONEY)
    # No CHECK: a computed actual result, not a bounded [0,1) setting.
    estimated_contribution_margin: Mapped[Decimal | None] = mapped_column(RATIO)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship()
    customer: Mapped[Customer | None] = relationship()
    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )
    status_history: Mapped[list[OrderStatusHistory]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )


class OrderLine(Base, CreatedAtMixin):
    __tablename__ = "order_lines"
    __table_args__ = (
        CheckConstraint("package_quantity > 0", name="ck_order_lines_package_quantity_positive"),
        CheckConstraint(
            "underlying_quantity > 0", name="ck_order_lines_underlying_quantity_positive"
        ),
        CheckConstraint(
            "charged_unit_price_snapshot >= 0",
            name="ck_order_lines_charged_unit_price_snapshot_non_negative",
        ),
        CheckConstraint("line_subtotal >= 0", name="ck_order_lines_line_subtotal_non_negative"),
        CheckConstraint(
            "packaging_cost_per_package_snapshot >= 0",
            name="ck_order_lines_packaging_cost_per_package_snapshot_non_negative",
        ),
        CheckConstraint(
            "packaging_cost_total_snapshot >= 0",
            name="ck_order_lines_packaging_cost_total_snapshot_non_negative",
        ),
        CheckConstraint(
            "custom_direct_cost_estimate IS NULL OR custom_direct_cost_estimate >= 0",
            name="ck_order_lines_custom_direct_cost_estimate_non_negative",
        ),
        CheckConstraint(
            "custom_active_time_minutes IS NULL OR custom_active_time_minutes >= 0",
            name="ck_order_lines_custom_active_time_minutes_non_negative",
        ),
        CheckConstraint(
            "(line_type = 'STANDARD_OPTION' AND product_id IS NOT NULL "
            "AND selling_option_id IS NOT NULL) "
            "OR (line_type = 'CUSTOM_QUANTITY' AND product_id IS NOT NULL "
            "AND selling_option_id IS NULL) "
            "OR (line_type = 'CUSTOM_ITEM' AND product_id IS NULL "
            "AND selling_option_id IS NULL)",
            name="ck_order_lines_line_type_shape",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    line_type: Mapped[OrderLineType] = mapped_column(
        pg_enum(OrderLineType, name="ck_order_lines_line_type"), nullable=False
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION")
    )
    selling_option_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("selling_options.id", ondelete="NO ACTION")
    )
    display_name_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    package_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    underlying_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    charged_unit_price_snapshot: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    packaging_cost_per_package_snapshot: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="0"
    )
    packaging_cost_total_snapshot: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="0"
    )
    price_override_reason: Mapped[str | None] = mapped_column(String)
    custom_direct_cost_estimate: Mapped[Decimal | None] = mapped_column(MONEY)
    custom_active_time_minutes: Mapped[int | None] = mapped_column(Integer)
    manual_fulfillment_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    manual_fulfillment_satisfied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    notes: Mapped[str | None] = mapped_column(Text)

    business: Mapped[Business] = relationship()
    order: Mapped[Order] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship()
    selling_option: Mapped[SellingOption | None] = relationship()


class Payment(Base, CreatedAtMixin):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_payments_amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Never enumerated by the spec (informal, business-specific) — plain VARCHAR.
    payment_method: Mapped[str] = mapped_column(String, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    business: Mapped[Business] = relationship()
    order: Mapped[Order] = relationship(back_populates="payments")


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[OrderStatus | None] = mapped_column(
        pg_enum(OrderStatus, name="ck_order_status_history_from_status")
    )
    to_status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, name="ck_order_status_history_to_status"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    business: Mapped[Business] = relationship()
    order: Mapped[Order] = relationship(back_populates="status_history")
