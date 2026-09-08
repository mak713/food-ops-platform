from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import QUANTITY
from app.db.enums import PurchasedInventoryTransactionType, pg_enum
from app.db.mixins import CreatedAtMixin, TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.product import Product


class PurchasedProductInventory(Base, TimestampMixin, VersionMixin):
    __tablename__ = "purchased_product_inventory"
    __table_args__ = (
        CheckConstraint(
            "physical_quantity >= 0",
            name="ck_purchased_product_inventory_physical_quantity_non_negative",
        ),
        CheckConstraint(
            "weighted_average_unit_cost >= 0",
            name="ck_purchased_inventory_wavg_unit_cost_nonneg",
        ),
        CheckConstraint(
            "latest_purchase_unit_cost IS NULL OR latest_purchase_unit_cost >= 0",
            name="ck_purchased_inventory_latest_purchase_cost_nonneg",
        ),
        CheckConstraint(
            "replacement_unit_cost IS NULL OR replacement_unit_cost >= 0",
            name="ck_purchased_inventory_replacement_cost_nonneg",
        ),
        UniqueConstraint(
            "business_id",
            "product_id",
            name="uq_purchased_product_inventory_business_id_product_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION"), nullable=False
    )
    physical_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False, server_default="0")
    weighted_average_unit_cost: Mapped[Decimal] = mapped_column(
        QUANTITY, nullable=False, server_default="0"
    )
    latest_purchase_unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    replacement_unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship()


class PurchasedProductInventoryTransaction(Base, CreatedAtMixin):
    __tablename__ = "purchased_product_inventory_transactions"
    __table_args__ = (
        CheckConstraint(
            "unit_cost IS NULL OR unit_cost >= 0",
            name="ck_purchased_inventory_txn_unit_cost_nonneg",
        ),
        CheckConstraint(
            "total_cost IS NULL OR total_cost >= 0",
            name="ck_purchased_inventory_txn_total_cost_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION"), nullable=False
    )
    transaction_type: Mapped[PurchasedInventoryTransactionType] = mapped_column(
        pg_enum(
            PurchasedInventoryTransactionType,
            name="ck_purchased_product_inventory_transactions_transaction_type",
        ),
        nullable=False,
    )
    # No CHECK: signed by design.
    quantity_change: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    total_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    supplier_text: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_lines.id", ondelete="SET NULL")
    )

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship()
