from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.columns import QUANTITY
from app.db.enums import UNIT_CODES
from app.db.mixins import CreatedAtMixin, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.business import Business
    from app.db.models.ingredient import Ingredient
    from app.db.models.product import Product

_UNIT_CODES_SQL = ", ".join(f"'{code}'" for code in sorted(UNIT_CODES))


class Recipe(Base, TimestampMixin):
    __tablename__ = "recipes"
    __table_args__ = (
        UniqueConstraint("business_id", "product_id", name="uq_recipes_business_id_product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    business: Mapped[Business] = relationship()
    product: Mapped[Product] = relationship(back_populates="recipe")
    revisions: Mapped[list[RecipeRevision]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", passive_deletes=True
    )


class RecipeRevision(Base, CreatedAtMixin):
    __tablename__ = "recipe_revisions"
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="ck_recipe_revisions_revision_number_positive"),
        CheckConstraint("yield_quantity > 0", name="ck_recipe_revisions_yield_quantity_positive"),
        CheckConstraint(
            "active_time_minutes >= 0",
            name="ck_recipe_revisions_active_time_minutes_non_negative",
        ),
        CheckConstraint(
            "elapsed_time_minutes IS NULL OR elapsed_time_minutes >= active_time_minutes",
            name="ck_recipe_revisions_elapsed_gte_active",
        ),
        UniqueConstraint(
            "recipe_id", "revision_number", name="uq_recipe_revisions_recipe_id_revision_number"
        ),
        Index(
            "ix_recipe_revisions_one_current",
            "recipe_id",
            unique=True,
            postgresql_where="is_current = true",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    yield_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    active_time_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_time_minutes: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    business: Mapped[Business] = relationship()
    recipe: Mapped[Recipe] = relationship(back_populates="revisions")
    ingredients: Mapped[list[RecipeRevisionIngredient]] = relationship(
        back_populates="recipe_revision", cascade="all, delete-orphan", passive_deletes=True
    )


class RecipeRevisionIngredient(Base):
    __tablename__ = "recipe_revision_ingredients"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_recipe_revision_ingredients_quantity_positive"),
        CheckConstraint(
            f"unit IN ({_UNIT_CODES_SQL})",
            name="ck_recipe_revision_ingredients_unit_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    recipe_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipe_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="NO ACTION"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)

    business: Mapped[Business] = relationship()
    recipe_revision: Mapped[RecipeRevision] = relationship(back_populates="ingredients")
    ingredient: Mapped[Ingredient] = relationship()
