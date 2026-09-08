from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import MONEY, QUANTITY
from app.db.enums import CalculationStatus, ProductionRunStatus, pg_enum
from app.db.mixins import CreatedAtMixin, TimestampMixin, VersionMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.ingredient import Ingredient
    from app.db.models.order import Order, OrderLine
    from app.db.models.product import Product
    from app.db.models.recipe import RecipeRevision
    from app.db.models.surplus import SurplusAllocation


class ProductionRequirement(Base, TimestampMixin):
    __tablename__ = "production_requirements"
    __table_args__ = (
        CheckConstraint(
            "confirmed_demand_quantity > 0",
            name="ck_production_requirements_confirmed_demand_quantity_positive",
        ),
        CheckConstraint(
            "surplus_allocated_quantity >= 0",
            name="ck_prod_reqs_surplus_allocated_qty_nonneg",
        ),
        CheckConstraint(
            "production_demand_quantity >= 0",
            name="ck_prod_reqs_production_demand_qty_nonneg",
        ),
        CheckConstraint(
            "recommended_batches IS NULL OR recommended_batches >= 0",
            name="ck_production_requirements_recommended_batches_non_negative",
        ),
        CheckConstraint(
            "expected_output_quantity IS NULL OR expected_output_quantity >= 0",
            name="ck_prod_reqs_expected_output_qty_nonneg",
        ),
        CheckConstraint(
            "expected_excess_quantity IS NULL OR expected_excess_quantity >= 0",
            name="ck_prod_reqs_expected_excess_qty_nonneg",
        ),
        CheckConstraint(
            "estimated_active_minutes IS NULL OR estimated_active_minutes >= 0",
            name="ck_prod_reqs_est_active_minutes_nonneg",
        ),
        CheckConstraint(
            "estimated_elapsed_minutes IS NULL OR estimated_elapsed_minutes >= 0",
            name="ck_prod_reqs_est_elapsed_minutes_nonneg",
        ),
        CheckConstraint(
            "estimated_ingredient_cost IS NULL OR estimated_ingredient_cost >= 0",
            name="ck_prod_reqs_est_ingredient_cost_nonneg",
        ),
        CheckConstraint(
            "estimated_labor_cost IS NULL OR estimated_labor_cost >= 0",
            name="ck_production_requirements_estimated_labor_cost_non_negative",
        ),
        CheckConstraint(
            "estimated_direct_production_cost IS NULL OR estimated_direct_production_cost >= 0",
            name="ck_prod_reqs_est_direct_prod_cost_nonneg",
        ),
        CheckConstraint(
            "(recipe_revision_id IS NULL AND calculation_status = 'INCOMPLETE_RECIPE') "
            "OR (recipe_revision_id IS NOT NULL AND calculation_status = 'CALCULATED')",
            name="ck_production_requirements_revision_status_match",
        ),
        Index(
            "ix_production_requirements_unique_calculated",
            "business_id",
            "product_id",
            "recipe_revision_id",
            "demand_date",
            unique=True,
            postgresql_where="recipe_revision_id IS NOT NULL",
        ),
        Index(
            "ix_production_requirements_unique_incomplete",
            "business_id",
            "product_id",
            "demand_date",
            unique=True,
            postgresql_where="recipe_revision_id IS NULL",
        ),
        Index("ix_production_requirements_business_id_demand_date", "business_id", "demand_date"),
        Index(
            "ix_production_requirements_business_id_product_id_demand_date",
            "business_id",
            "product_id",
            "demand_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION"), nullable=False
    )
    recipe_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipe_revisions.id", ondelete="NO ACTION")
    )
    demand_date: Mapped[date] = mapped_column(Date, nullable=False)
    calculation_status: Mapped[CalculationStatus] = mapped_column(
        pg_enum(CalculationStatus, name="ck_production_requirements_calculation_status"),
        nullable=False,
    )
    confirmed_demand_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    surplus_allocated_quantity: Mapped[Decimal] = mapped_column(
        QUANTITY, nullable=False, server_default="0"
    )
    production_demand_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    recommended_batches: Mapped[int | None] = mapped_column(Integer)
    expected_output_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    expected_excess_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    estimated_active_minutes: Mapped[int | None] = mapped_column(Integer)
    estimated_elapsed_minutes: Mapped[int | None] = mapped_column(Integer)
    suggested_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_ingredient_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    estimated_labor_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    estimated_direct_production_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship()
    recipe_revision: Mapped[RecipeRevision | None] = relationship()
    order_links: Mapped[list[ProductionRequirementOrder]] = relationship(
        back_populates="production_requirement", cascade="all, delete-orphan", passive_deletes=True
    )
    ingredient_requirements: Mapped[list[ProductionIngredientRequirement]] = relationship(
        back_populates="production_requirement", cascade="all, delete-orphan", passive_deletes=True
    )
    ingredient_reservations: Mapped[list[IngredientReservation]] = relationship(
        back_populates="production_requirement", cascade="all, delete-orphan", passive_deletes=True
    )
    surplus_allocations: Mapped[list[SurplusAllocation]] = relationship(
        back_populates="production_requirement", cascade="all, delete-orphan", passive_deletes=True
    )


class ProductionRequirementOrder(Base):
    __tablename__ = "production_requirement_orders"
    __table_args__ = (
        CheckConstraint(
            "demand_quantity > 0",
            name="ck_production_requirement_orders_demand_quantity_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
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
    demand_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)

    business: Mapped[Business] = relationship()
    production_requirement: Mapped[ProductionRequirement] = relationship(
        back_populates="order_links"
    )
    order: Mapped[Order] = relationship()
    order_line: Mapped[OrderLine] = relationship()


class ProductionIngredientRequirement(Base):
    __tablename__ = "production_ingredient_requirements"
    __table_args__ = (
        CheckConstraint(
            "required_quantity_canonical > 0",
            name="ck_prod_ingredient_reqs_required_qty_canonical_positive",
        ),
        CheckConstraint(
            "estimated_unit_cost IS NULL OR estimated_unit_cost >= 0",
            name="ck_prod_ingredient_reqs_est_unit_cost_nonneg",
        ),
        CheckConstraint(
            "estimated_total_cost IS NULL OR estimated_total_cost >= 0",
            name="ck_prod_ingredient_reqs_est_total_cost_nonneg",
        ),
        UniqueConstraint(
            "production_requirement_id",
            "ingredient_id",
            name="uq_prod_ingredient_reqs_requirement_id_ingredient_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    production_requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_requirements.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="NO ACTION"), nullable=False
    )
    required_quantity_canonical: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    estimated_unit_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    estimated_total_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)

    business: Mapped[Business] = relationship()
    production_requirement: Mapped[ProductionRequirement] = relationship(
        back_populates="ingredient_requirements"
    )
    ingredient: Mapped[Ingredient] = relationship()


class IngredientReservation(Base, TimestampMixin):
    __tablename__ = "ingredient_reservations"
    __table_args__ = (
        CheckConstraint(
            "quantity_canonical > 0", name="ck_ingredient_reservations_quantity_canonical_positive"
        ),
        UniqueConstraint(
            "production_requirement_id",
            "ingredient_id",
            name="uq_ingredient_reservations_requirement_id_ingredient_id",
        ),
        Index(
            "ix_ingredient_reservations_business_id_ingredient_id",
            "business_id",
            "ingredient_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    production_requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_requirements.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="NO ACTION"), nullable=False
    )
    quantity_canonical: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)

    business: Mapped[Business] = relationship()
    production_requirement: Mapped[ProductionRequirement] = relationship(
        back_populates="ingredient_reservations"
    )
    ingredient: Mapped[Ingredient] = relationship()


class ProductionRun(Base, TimestampMixin, VersionMixin):
    __tablename__ = "production_runs"
    __table_args__ = (
        CheckConstraint("planned_batches > 0", name="ck_production_runs_planned_batches_positive"),
        CheckConstraint(
            "planned_output_quantity > 0",
            name="ck_production_runs_planned_output_quantity_positive",
        ),
        CheckConstraint(
            "actual_output_quantity IS NULL OR actual_output_quantity >= 0",
            name="ck_production_runs_actual_output_quantity_non_negative",
        ),
        CheckConstraint(
            "recipe_yield_snapshot > 0", name="ck_production_runs_recipe_yield_snapshot_positive"
        ),
        CheckConstraint(
            "active_minutes_per_batch_snapshot >= 0",
            name="ck_prod_runs_active_min_per_batch_snapshot_nonneg",
        ),
        CheckConstraint(
            "elapsed_minutes_per_batch_snapshot IS NULL "
            "OR elapsed_minutes_per_batch_snapshot >= active_minutes_per_batch_snapshot",
            name="ck_production_runs_elapsed_gte_active_snapshot",
        ),
        CheckConstraint(
            "labor_rate_snapshot >= 0", name="ck_production_runs_labor_rate_snapshot_non_negative"
        ),
        CheckConstraint(
            "estimated_ingredient_cost >= 0",
            name="ck_production_runs_estimated_ingredient_cost_non_negative",
        ),
        CheckConstraint(
            "estimated_labor_cost >= 0",
            name="ck_production_runs_estimated_labor_cost_non_negative",
        ),
        CheckConstraint(
            "estimated_total_production_cost >= 0",
            name="ck_production_runs_estimated_total_production_cost_non_negative",
        ),
        CheckConstraint(
            "actual_ingredient_cost IS NULL OR actual_ingredient_cost >= 0",
            name="ck_production_runs_actual_ingredient_cost_non_negative",
        ),
        CheckConstraint(
            "actual_labor_cost IS NULL OR actual_labor_cost >= 0",
            name="ck_production_runs_actual_labor_cost_non_negative",
        ),
        CheckConstraint(
            "actual_total_production_cost IS NULL OR actual_total_production_cost >= 0",
            name="ck_production_runs_actual_total_production_cost_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    source_production_requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_requirements.id", ondelete="SET NULL")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="NO ACTION"), nullable=False
    )
    recipe_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipe_revisions.id", ondelete="NO ACTION"), nullable=False
    )
    status: Mapped[ProductionRunStatus] = mapped_column(
        pg_enum(ProductionRunStatus, name="ck_production_runs_status"), nullable=False
    )
    planned_batches: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_output_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    actual_output_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    recipe_yield_snapshot: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    active_minutes_per_batch_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_minutes_per_batch_snapshot: Mapped[int | None] = mapped_column(Integer)
    labor_rate_snapshot: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    estimated_ingredient_cost: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    estimated_labor_cost: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    estimated_total_production_cost: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    actual_ingredient_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    actual_labor_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    actual_total_production_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship()
    source_production_requirement: Mapped[ProductionRequirement | None] = relationship()
    product: Mapped[Product] = relationship()
    recipe_revision: Mapped[RecipeRevision] = relationship()
    run_ingredients: Mapped[list[ProductionRunIngredient]] = relationship(
        back_populates="production_run", cascade="all, delete-orphan", passive_deletes=True
    )
    order_allocations: Mapped[list[ProductionRunOrderAllocation]] = relationship(
        back_populates="production_run", cascade="all, delete-orphan", passive_deletes=True
    )


class ProductionRunIngredient(Base):
    __tablename__ = "production_run_ingredients"
    __table_args__ = (
        CheckConstraint(
            "planned_quantity > 0", name="ck_production_run_ingredients_planned_quantity_positive"
        ),
        CheckConstraint(
            "actual_quantity IS NULL OR actual_quantity >= 0",
            name="ck_production_run_ingredients_actual_quantity_non_negative",
        ),
        CheckConstraint(
            "weighted_average_unit_cost_snapshot >= 0",
            name="ck_prod_run_ingr_wavg_unit_cost_snapshot_nonneg",
        ),
        CheckConstraint(
            "replacement_unit_cost_snapshot IS NULL OR replacement_unit_cost_snapshot >= 0",
            name="ck_prod_run_ingr_replacement_cost_snapshot_nonneg",
        ),
        CheckConstraint(
            "actual_unit_cost_basis IS NULL OR actual_unit_cost_basis >= 0",
            name="ck_prod_run_ingr_actual_cost_basis_nonneg",
        ),
        CheckConstraint(
            "actual_total_cost IS NULL OR actual_total_cost >= 0",
            name="ck_production_run_ingredients_actual_total_cost_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_runs.id", ondelete="CASCADE"), nullable=False
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="NO ACTION"), nullable=False
    )
    planned_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    actual_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    weighted_average_unit_cost_snapshot: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    replacement_unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(QUANTITY)
    actual_unit_cost_basis: Mapped[Decimal | None] = mapped_column(QUANTITY)
    actual_total_cost: Mapped[Decimal | None] = mapped_column(QUANTITY)

    business: Mapped[Business] = relationship()
    production_run: Mapped[ProductionRun] = relationship(back_populates="run_ingredients")
    ingredient: Mapped[Ingredient] = relationship()


class ProductionRunOrderAllocation(Base, CreatedAtMixin):
    __tablename__ = "production_run_order_allocations"
    __table_args__ = (
        CheckConstraint(
            "quantity > 0", name="ck_production_run_order_allocations_quantity_positive"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_runs.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="NO ACTION"), nullable=False
    )
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_lines.id", ondelete="NO ACTION"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)

    business: Mapped[Business] = relationship()
    production_run: Mapped[ProductionRun] = relationship(back_populates="order_allocations")
    order: Mapped[Order] = relationship()
    order_line: Mapped[OrderLine] = relationship()
