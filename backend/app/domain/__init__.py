"""Deterministic, pure domain calculators (Spec §11.3): no HTTP or database access.

Each module here is a focused, side-effect-free component (`UnitConversion`,
`RecipeScaling`, and later phases' `InventoryRequirement`/`SurplusAllocation`/
`ProductionPlanning`/`CostCalculation`) that the service layer composes with real data.
"""
