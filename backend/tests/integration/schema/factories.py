"""Minimal row-builder helpers for Phase 1 schema tests.

Every factory assigns `id=uuid.uuid4()` explicitly at construction time (rather
than relying on the column's Python-side default, which only fires at flush) so
callers can chain factory calls and reference `.id` before flushing. Factories
`session.add()` the object but do not flush — call `session.flush()` when a test
wants to trigger real constraint/FK evaluation.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.enums import (
    CalculationStatus,
    MeasurementFamily,
    OrderLineType,
    OrderStatus,
    ProductionRunStatus,
    ProductType,
)
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

_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter}"


def make_user(session: Session, **overrides) -> User:
    obj = User(
        id=uuid.uuid4(),
        name=overrides.get("name", "Test Owner"),
        email=overrides.get("email", f"{_unique('owner')}@example.com"),
        password_hash=overrides.get("password_hash", "not-a-real-hash"),
    )
    session.add(obj)
    return obj


def make_business(session: Session, owner: User, **overrides) -> Business:
    obj = Business(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name=overrides.get("name", "Test Business"),
        timezone=overrides.get("timezone", "America/New_York"),
        fulfillment_warning_minutes=overrides.get("fulfillment_warning_minutes", 60),
        shopping_horizon_days=overrides.get("shopping_horizon_days", 7),
    )
    session.add(obj)
    return obj


def make_business_graph(session: Session, **overrides) -> Business:
    """Convenience: a User + their Business in one call."""
    user = make_user(session)
    return make_business(session, user, **overrides)


def make_customer(session: Session, business: Business, **overrides) -> Customer:
    obj = Customer(
        id=uuid.uuid4(),
        business_id=business.id,
        name=overrides.get("name", "Test Customer"),
    )
    session.add(obj)
    return obj


def make_product(
    session: Session,
    business: Business,
    product_type: ProductType = ProductType.PRODUCED,
    **overrides,
) -> Product:
    obj = Product(
        id=uuid.uuid4(),
        business_id=business.id,
        name=overrides.get("name", "Test Product"),
        product_type=product_type,
    )
    session.add(obj)
    return obj


def make_selling_option(
    session: Session, business: Business, product: Product, **overrides
) -> SellingOption:
    obj = SellingOption(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        name=overrides.get("name", "6-pack"),
        quantity_units=overrides.get("quantity_units", 6),
        price=overrides.get("price", "12.00"),
    )
    session.add(obj)
    return obj


def make_recipe(session: Session, business: Business, product: Product, **overrides) -> Recipe:
    obj = Recipe(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        name=overrides.get("name", "Test Recipe"),
    )
    session.add(obj)
    return obj


def make_recipe_revision(
    session: Session, business: Business, recipe: Recipe, **overrides
) -> RecipeRevision:
    obj = RecipeRevision(
        id=uuid.uuid4(),
        business_id=business.id,
        recipe_id=recipe.id,
        revision_number=overrides.get("revision_number", 1),
        yield_quantity=overrides.get("yield_quantity", 12),
        active_time_minutes=overrides.get("active_time_minutes", 30),
        is_current=overrides.get("is_current", True),
    )
    session.add(obj)
    return obj


def make_ingredient(session: Session, business: Business, **overrides) -> Ingredient:
    obj = Ingredient(
        id=uuid.uuid4(),
        business_id=business.id,
        name=overrides.get("name", "Flour"),
        measurement_family=overrides.get("measurement_family", MeasurementFamily.WEIGHT),
        canonical_unit=overrides.get("canonical_unit", "g"),
    )
    session.add(obj)
    return obj


def make_recipe_revision_ingredient(
    session: Session,
    business: Business,
    recipe_revision: RecipeRevision,
    ingredient: Ingredient,
    **overrides,
) -> RecipeRevisionIngredient:
    obj = RecipeRevisionIngredient(
        id=uuid.uuid4(),
        business_id=business.id,
        recipe_revision_id=recipe_revision.id,
        ingredient_id=ingredient.id,
        quantity=overrides.get("quantity", 500),
        unit=overrides.get("unit", "g"),
    )
    session.add(obj)
    return obj


def make_inventory_transaction(
    session: Session, business: Business, ingredient: Ingredient, **overrides
) -> InventoryTransaction:
    obj = InventoryTransaction(
        id=uuid.uuid4(),
        business_id=business.id,
        ingredient_id=ingredient.id,
        transaction_type=overrides.get("transaction_type", "INITIAL_BALANCE"),
        quantity_change=overrides.get("quantity_change", 1000),
    )
    session.add(obj)
    return obj


def make_purchased_inventory(
    session: Session, business: Business, product: Product, **overrides
) -> PurchasedProductInventory:
    obj = PurchasedProductInventory(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        physical_quantity=overrides.get("physical_quantity", 10),
    )
    session.add(obj)
    return obj


def make_purchased_inventory_transaction(
    session: Session, business: Business, product: Product, **overrides
) -> PurchasedProductInventoryTransaction:
    obj = PurchasedProductInventoryTransaction(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        transaction_type=overrides.get("transaction_type", "INITIAL_BALANCE"),
        quantity_change=overrides.get("quantity_change", 10),
    )
    session.add(obj)
    return obj


def make_order(session: Session, business: Business, **overrides) -> Order:
    obj = Order(
        id=uuid.uuid4(),
        business_id=business.id,
        order_number=overrides.get("order_number", _unique("ORD")),
        status=overrides.get("status", OrderStatus.DRAFT),
    )
    session.add(obj)
    return obj


def make_order_line(
    session: Session,
    business: Business,
    order: Order,
    line_type: OrderLineType = OrderLineType.CUSTOM_ITEM,
    product: Product | None = None,
    selling_option: SellingOption | None = None,
    **overrides,
) -> OrderLine:
    obj = OrderLine(
        id=uuid.uuid4(),
        business_id=business.id,
        order_id=order.id,
        line_type=line_type,
        product_id=product.id if product else None,
        selling_option_id=selling_option.id if selling_option else None,
        display_name_snapshot=overrides.get("display_name_snapshot", "Test Line"),
        package_quantity=overrides.get("package_quantity", 1),
        underlying_quantity=overrides.get("underlying_quantity", 1),
        charged_unit_price_snapshot=overrides.get("charged_unit_price_snapshot", "10.00"),
        line_subtotal=overrides.get("line_subtotal", "10.00"),
    )
    session.add(obj)
    return obj


def make_payment(session: Session, business: Business, order: Order, **overrides) -> Payment:
    obj = Payment(
        id=uuid.uuid4(),
        business_id=business.id,
        order_id=order.id,
        amount=overrides.get("amount", "10.00"),
        payment_method=overrides.get("payment_method", "cash"),
        payment_date=overrides.get("payment_date", date.today()),
    )
    session.add(obj)
    return obj


def make_order_status_history(
    session: Session, business: Business, order: Order, **overrides
) -> OrderStatusHistory:
    obj = OrderStatusHistory(
        id=uuid.uuid4(),
        business_id=business.id,
        order_id=order.id,
        to_status=overrides.get("to_status", OrderStatus.DRAFT),
        changed_at=overrides.get("changed_at", datetime.now(UTC)),
    )
    session.add(obj)
    return obj


def make_production_requirement(
    session: Session,
    business: Business,
    product: Product,
    recipe_revision: RecipeRevision | None,
    **overrides,
) -> ProductionRequirement:
    calc_status = overrides.get(
        "calculation_status",
        CalculationStatus.CALCULATED if recipe_revision else CalculationStatus.INCOMPLETE_RECIPE,
    )
    obj = ProductionRequirement(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        recipe_revision_id=recipe_revision.id if recipe_revision else None,
        demand_date=overrides.get("demand_date", date.today()),
        calculation_status=calc_status,
        confirmed_demand_quantity=overrides.get("confirmed_demand_quantity", 12),
        production_demand_quantity=overrides.get("production_demand_quantity", 12),
    )
    session.add(obj)
    return obj


def make_production_requirement_order(
    session: Session,
    business: Business,
    production_requirement: ProductionRequirement,
    order: Order,
    order_line: OrderLine,
    **overrides,
) -> ProductionRequirementOrder:
    obj = ProductionRequirementOrder(
        id=uuid.uuid4(),
        business_id=business.id,
        production_requirement_id=production_requirement.id,
        order_id=order.id,
        order_line_id=order_line.id,
        demand_quantity=overrides.get("demand_quantity", 12),
    )
    session.add(obj)
    return obj


def make_production_ingredient_requirement(
    session: Session,
    business: Business,
    production_requirement: ProductionRequirement,
    ingredient: Ingredient,
    **overrides,
) -> ProductionIngredientRequirement:
    obj = ProductionIngredientRequirement(
        id=uuid.uuid4(),
        business_id=business.id,
        production_requirement_id=production_requirement.id,
        ingredient_id=ingredient.id,
        required_quantity_canonical=overrides.get("required_quantity_canonical", 500),
    )
    session.add(obj)
    return obj


def make_ingredient_reservation(
    session: Session,
    business: Business,
    production_requirement: ProductionRequirement,
    ingredient: Ingredient,
    **overrides,
) -> IngredientReservation:
    obj = IngredientReservation(
        id=uuid.uuid4(),
        business_id=business.id,
        production_requirement_id=production_requirement.id,
        ingredient_id=ingredient.id,
        quantity_canonical=overrides.get("quantity_canonical", 500),
    )
    session.add(obj)
    return obj


def make_production_run(
    session: Session,
    business: Business,
    product: Product,
    recipe_revision: RecipeRevision,
    **overrides,
) -> ProductionRun:
    obj = ProductionRun(
        id=uuid.uuid4(),
        business_id=business.id,
        source_production_requirement_id=overrides.get("source_production_requirement_id"),
        product_id=product.id,
        recipe_revision_id=recipe_revision.id,
        status=overrides.get("status", ProductionRunStatus.IN_PRODUCTION),
        planned_batches=overrides.get("planned_batches", 1),
        planned_output_quantity=overrides.get("planned_output_quantity", 12),
        recipe_yield_snapshot=overrides.get("recipe_yield_snapshot", 12),
        active_minutes_per_batch_snapshot=overrides.get("active_minutes_per_batch_snapshot", 30),
        labor_rate_snapshot=overrides.get("labor_rate_snapshot", "15.00"),
        estimated_ingredient_cost=overrides.get("estimated_ingredient_cost", 5),
        estimated_labor_cost=overrides.get("estimated_labor_cost", Decimal("7.5")),
        estimated_total_production_cost=overrides.get(
            "estimated_total_production_cost", Decimal("12.5")
        ),
        started_at=overrides.get("started_at", datetime.now(UTC)),
    )
    session.add(obj)
    return obj


def make_production_run_ingredient(
    session: Session,
    business: Business,
    production_run: ProductionRun,
    ingredient: Ingredient,
    **overrides,
) -> ProductionRunIngredient:
    obj = ProductionRunIngredient(
        id=uuid.uuid4(),
        business_id=business.id,
        production_run_id=production_run.id,
        ingredient_id=ingredient.id,
        planned_quantity=overrides.get("planned_quantity", 500),
        weighted_average_unit_cost_snapshot=overrides.get(
            "weighted_average_unit_cost_snapshot", "0.01"
        ),
    )
    session.add(obj)
    return obj


def make_production_run_order_allocation(
    session: Session,
    business: Business,
    production_run: ProductionRun,
    order: Order,
    order_line: OrderLine,
    **overrides,
) -> ProductionRunOrderAllocation:
    obj = ProductionRunOrderAllocation(
        id=uuid.uuid4(),
        business_id=business.id,
        production_run_id=production_run.id,
        order_id=order.id,
        order_line_id=order_line.id,
        quantity=overrides.get("quantity", 12),
    )
    session.add(obj)
    return obj


def make_surplus_inventory(
    session: Session, business: Business, product: Product, **overrides
) -> SurplusInventory:
    obj = SurplusInventory(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        source_production_run_id=overrides.get("source_production_run_id"),
        physical_quantity=overrides.get("physical_quantity", 6),
        unit_cost_basis=overrides.get("unit_cost_basis", "0.50"),
        produced_at=overrides.get("produced_at", datetime.now(UTC)),
        is_reusable=overrides.get("is_reusable", True),
    )
    session.add(obj)
    return obj


def make_surplus_allocation(
    session: Session,
    business: Business,
    surplus_inventory: SurplusInventory,
    production_requirement: ProductionRequirement,
    order: Order,
    order_line: OrderLine,
    **overrides,
) -> SurplusAllocation:
    obj = SurplusAllocation(
        id=uuid.uuid4(),
        business_id=business.id,
        surplus_inventory_id=surplus_inventory.id,
        production_requirement_id=production_requirement.id,
        order_id=order.id,
        order_line_id=order_line.id,
        quantity=overrides.get("quantity", 6),
    )
    session.add(obj)
    return obj


def make_surplus_transaction(
    session: Session, business: Business, surplus_inventory: SurplusInventory, **overrides
) -> SurplusTransaction:
    obj = SurplusTransaction(
        id=uuid.uuid4(),
        business_id=business.id,
        surplus_inventory_id=surplus_inventory.id,
        transaction_type=overrides.get("transaction_type", "PRODUCTION_OUTPUT"),
        quantity_change=overrides.get("quantity_change", 6),
    )
    session.add(obj)
    return obj


def make_purchased_product_reservation(
    session: Session,
    business: Business,
    product: Product,
    order: Order,
    order_line: OrderLine,
    **overrides,
) -> PurchasedProductReservation:
    obj = PurchasedProductReservation(
        id=uuid.uuid4(),
        business_id=business.id,
        product_id=product.id,
        order_id=order.id,
        order_line_id=order_line.id,
        quantity=overrides.get("quantity", 1),
    )
    session.add(obj)
    return obj


def make_order_cost_allocation(
    session: Session, business: Business, order: Order, **overrides
) -> OrderCostAllocation:
    obj = OrderCostAllocation(
        id=uuid.uuid4(),
        business_id=business.id,
        order_id=order.id,
        order_line_id=overrides.get("order_line_id"),
        cost_type=overrides.get("cost_type", "PACKAGING"),
        amount=overrides.get("amount", "1.00"),
        production_run_order_allocation_id=overrides.get("production_run_order_allocation_id"),
        surplus_inventory_id=overrides.get("surplus_inventory_id"),
        purchased_inventory_transaction_id=overrides.get("purchased_inventory_transaction_id"),
    )
    session.add(obj)
    return obj
