from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import MONEY
from app.db.enums import CostType, pg_enum
from app.db.mixins import CreatedAtMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.order import Order, OrderLine
    from app.db.models.production import ProductionRunOrderAllocation
    from app.db.models.purchased_inventory import PurchasedProductInventoryTransaction
    from app.db.models.surplus import SurplusInventory


class OrderCostAllocation(Base, CreatedAtMixin):
    __tablename__ = "order_cost_allocations"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_order_cost_allocations_amount_non_negative"),
        CheckConstraint(
            "(cost_type = 'PRODUCTION' AND production_run_order_allocation_id IS NOT NULL "
            "AND surplus_inventory_id IS NULL AND purchased_inventory_transaction_id IS NULL) "
            "OR (cost_type = 'SURPLUS' AND surplus_inventory_id IS NOT NULL "
            "AND production_run_order_allocation_id IS NULL "
            "AND purchased_inventory_transaction_id IS NULL) "
            "OR (cost_type = 'PURCHASED_GOOD' AND purchased_inventory_transaction_id IS NOT NULL "
            "AND production_run_order_allocation_id IS NULL AND surplus_inventory_id IS NULL) "
            "OR (cost_type IN ('PACKAGING', 'CUSTOM_DIRECT') "
            "AND production_run_order_allocation_id IS NULL AND surplus_inventory_id IS NULL "
            "AND purchased_inventory_transaction_id IS NULL)",
            name="ck_order_cost_allocations_source_exclusivity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="NO ACTION"), nullable=False
    )
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_lines.id", ondelete="NO ACTION")
    )
    cost_type: Mapped[CostType] = mapped_column(
        pg_enum(CostType, name="ck_order_cost_allocations_cost_type"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    production_run_order_allocation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_run_order_allocations.id", ondelete="NO ACTION"),
    )
    surplus_inventory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("surplus_inventory.id", ondelete="NO ACTION")
    )
    purchased_inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("purchased_product_inventory_transactions.id", ondelete="NO ACTION"),
    )
    description: Mapped[str | None] = mapped_column(String)

    business: Mapped[Business] = relationship()
    order: Mapped[Order] = relationship()
    order_line: Mapped[OrderLine | None] = relationship()
    production_run_order_allocation: Mapped[ProductionRunOrderAllocation | None] = relationship()
    surplus_inventory: Mapped[SurplusInventory | None] = relationship()
    purchased_inventory_transaction: Mapped[PurchasedProductInventoryTransaction | None] = (
        relationship()
    )
