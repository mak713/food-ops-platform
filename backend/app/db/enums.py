import enum

import sqlalchemy as sa


class MeasurementFamily(enum.StrEnum):
    WEIGHT = "WEIGHT"
    VOLUME = "VOLUME"
    COUNT = "COUNT"


class ProductType(enum.StrEnum):
    PRODUCED = "PRODUCED"
    PURCHASED = "PURCHASED"


class FulfillmentMethod(enum.StrEnum):
    PICKUP = "PICKUP"
    DELIVERY = "DELIVERY"
    OTHER = "OTHER"


class InventoryTransactionType(enum.StrEnum):
    INITIAL_BALANCE = "INITIAL_BALANCE"
    RESTOCK = "RESTOCK"
    PRODUCTION_CONSUMPTION = "PRODUCTION_CONSUMPTION"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"


class PurchasedInventoryTransactionType(enum.StrEnum):
    INITIAL_BALANCE = "INITIAL_BALANCE"
    RESTOCK = "RESTOCK"
    ORDER_FULFILLMENT = "ORDER_FULFILLMENT"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"


class OrderStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    READY = "READY"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class OrderLineType(enum.StrEnum):
    STANDARD_OPTION = "STANDARD_OPTION"
    CUSTOM_QUANTITY = "CUSTOM_QUANTITY"
    CUSTOM_ITEM = "CUSTOM_ITEM"


class CalculationStatus(enum.StrEnum):
    CALCULATED = "CALCULATED"
    INCOMPLETE_RECIPE = "INCOMPLETE_RECIPE"


class ProductionRunStatus(enum.StrEnum):
    IN_PRODUCTION = "IN_PRODUCTION"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class SurplusTransactionType(enum.StrEnum):
    PRODUCTION_OUTPUT = "PRODUCTION_OUTPUT"
    ORDER_FULFILLMENT = "ORDER_FULFILLMENT"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"


class CostType(enum.StrEnum):
    PRODUCTION = "PRODUCTION"
    SURPLUS = "SURPLUS"
    PURCHASED_GOOD = "PURCHASED_GOOD"
    PACKAGING = "PACKAGING"
    CUSTOM_DIRECT = "CUSTOM_DIRECT"


def pg_enum(enum_cls: type[enum.Enum], *, name: str) -> sa.Enum:
    """VARCHAR + CHECK-backed enum column type (not a native PostgreSQL ENUM), per the
    Phase 1 design decision: native enums make adding a value later require
    ALTER TYPE ... ADD VALUE, which can't run in the same transaction as other DDL and
    can never be removed. `create_constraint=True` is required — `native_enum=False`
    alone does not emit the CHECK. `values_callable` ensures the persisted string (and
    the generated CHECK's IN (...) list) is sourced from each member's `.value`, not its
    Python attribute `.name`.
    """
    longest_value = max(len(member.value) for member in enum_cls)
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
        length=longest_value,
    )


# Closed universe of unit codes across all measurement families (Spec §7.5). Used for
# the partial DB-level guard on ingredients.canonical_unit / recipe_revision_ingredients.unit.
# Full family-compatibility (e.g. "g" only valid for a WEIGHT ingredient) is cross-table
# and enforced at the application layer, not here.
UNIT_CODES = {
    "g",
    "kg",
    "oz",
    "lb",  # WEIGHT
    "mL",
    "L",
    "tsp",
    "tbsp",
    "cup",
    "fl_oz",  # VOLUME
    "each",  # COUNT
}
