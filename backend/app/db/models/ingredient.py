from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.db.base import Base
from app.db.columns import QUANTITY
from app.db.enums import UNIT_CODES, InventoryTransactionType, MeasurementFamily, pg_enum
from app.db.mixins import CreatedAtMixin, TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business

_UNIT_CODES_SQL = ", ".join(f"'{code}'" for code in sorted(UNIT_CODES))


class Ingredient(Base, TimestampMixin, VersionMixin):
    __tablename__ = "ingredients"
    __table_args__ = (
        CheckConstraint(
            f"canonical_unit IN ({_UNIT_CODES_SQL})",
            name="ck_ingredients_canonical_unit_valid",
        ),
        CheckConstraint(
            "weighted_average_unit_cost >= 0",
            name="ck_ingredients_weighted_average_unit_cost_non_negative",
        ),
        CheckConstraint(
            "latest_purchase_unit_cost IS NULL OR latest_purchase_unit_cost >= 0",
            name="ck_ingredients_latest_purchase_unit_cost_non_negative",
        ),
        CheckConstraint(
            "replacement_unit_cost IS NULL OR replacement_unit_cost >= 0",
            name="ck_ingredients_replacement_unit_cost_non_negative",
        ),
        Index("ix_ingredients_business_id_is_active", "business_id", "is_active"),
    )

    # Optimistic concurrency (Spec §8.33 explicitly names Ingredient; Phase 4 Plan v4 §4) —
    # applied per-class, not on VersionMixin itself, matching the Product/SellingOption
    # precedent exactly. The `version` column itself already existed (VersionMixin, Phase
    # 1) but was never wired into `__mapper_args__` until now — this is a pure ORM-mapper
    # change, no migration.
    @declared_attr.directive
    def __mapper_args__(cls) -> dict:
        return {"version_id_col": cls.version}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    measurement_family: Mapped[MeasurementFamily] = mapped_column(
        pg_enum(MeasurementFamily, name="ck_ingredients_measurement_family"), nullable=False
    )
    canonical_unit: Mapped[str] = mapped_column(String, nullable=False)
    # No CHECK: may legitimately go negative after real overconsumption (INV-009).
    physical_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False, server_default="0")
    weighted_average_unit_cost: Mapped[Decimal] = mapped_column(
        QUANTITY, nullable=False, server_default="0"
    )
    latest_purchase_unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    replacement_unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    business: Mapped[Business] = relationship()


class InventoryTransaction(Base, CreatedAtMixin):
    __tablename__ = "inventory_transactions"
    __table_args__ = (
        CheckConstraint(
            "unit_cost IS NULL OR unit_cost >= 0",
            name="ck_inventory_transactions_unit_cost_non_negative",
        ),
        CheckConstraint(
            "total_cost IS NULL OR total_cost >= 0",
            name="ck_inventory_transactions_total_cost_non_negative",
        ),
        Index(
            "ix_inventory_transactions_business_id_ingredient_id_created_at",
            "business_id",
            "ingredient_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="NO ACTION"), nullable=False
    )
    transaction_type: Mapped[InventoryTransactionType] = mapped_column(
        pg_enum(InventoryTransactionType, name="ck_inventory_transactions_transaction_type"),
        nullable=False,
    )
    # No CHECK: signed by design (restock positive, consumption negative).
    quantity_change: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    total_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    supplier_text: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    production_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_runs.id", ondelete="SET NULL")
    )

    business: Mapped[Business] = relationship()
    ingredient: Mapped[Ingredient] = relationship()
