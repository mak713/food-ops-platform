"""Importing this package registers every model on Base.metadata — required for
Alembic autogenerate and for migrations/env.py's target_metadata to see the full
domain schema."""

from app.db.models.auth import AuthSession, PasswordResetToken
from app.db.models.business import Business
from app.db.models.cost import OrderCostAllocation
from app.db.models.customer import Customer
from app.db.models.ingredient import Ingredient, InventoryTransaction
from app.db.models.order import Order, OrderLine, OrderStatusHistory, Payment
from app.db.models.product import Product, SellingOption
from app.db.models.production import (
    IngredientReservation,
    ProductionIngredientRequirement,
    ProductionRequirement,
    ProductionRequirementOrder,
    ProductionRun,
    ProductionRunIngredient,
    ProductionRunOrderAllocation,
)
from app.db.models.purchased_inventory import (
    PurchasedProductInventory,
    PurchasedProductInventoryTransaction,
)
from app.db.models.recipe import Recipe, RecipeRevision, RecipeRevisionIngredient
from app.db.models.surplus import (
    PurchasedProductReservation,
    SurplusAllocation,
    SurplusInventory,
    SurplusTransaction,
)
from app.db.models.user import User

__all__ = [
    "AuthSession",
    "Business",
    "Customer",
    "Ingredient",
    "IngredientReservation",
    "InventoryTransaction",
    "Order",
    "OrderCostAllocation",
    "OrderLine",
    "OrderStatusHistory",
    "PasswordResetToken",
    "Payment",
    "Product",
    "ProductionIngredientRequirement",
    "ProductionRequirement",
    "ProductionRequirementOrder",
    "ProductionRun",
    "ProductionRunIngredient",
    "ProductionRunOrderAllocation",
    "PurchasedProductInventory",
    "PurchasedProductInventoryTransaction",
    "PurchasedProductReservation",
    "Recipe",
    "RecipeRevision",
    "RecipeRevisionIngredient",
    "SellingOption",
    "SurplusAllocation",
    "SurplusInventory",
    "SurplusTransaction",
    "User",
]
