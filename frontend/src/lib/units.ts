// Mirrors the backend's closed unit set (backend/app/db/enums.py UNIT_CODES, Spec §7.5) —
// grouped by measurement family so both the Ingredient form (canonical_unit) and the
// Recipe ingredient-line editor (per-line unit, filtered to the chosen Ingredient's
// family) can offer only valid choices, never inventing units the backend doesn't accept.

import type { MeasurementFamily } from "../features/ingredients/api";

export const UNIT_OPTIONS_BY_FAMILY: Record<MeasurementFamily, { value: string; label: string }[]> = {
  WEIGHT: [
    { value: "g", label: "g (grams)" },
    { value: "kg", label: "kg (kilograms)" },
    { value: "oz", label: "oz (ounces)" },
    { value: "lb", label: "lb (pounds)" },
  ],
  VOLUME: [
    { value: "mL", label: "mL (milliliters)" },
    { value: "L", label: "L (liters)" },
    { value: "tsp", label: "tsp (teaspoons)" },
    { value: "tbsp", label: "tbsp (tablespoons)" },
    { value: "fl_oz", label: "fl oz (fluid ounces)" },
    { value: "cup", label: "cup" },
  ],
  COUNT: [{ value: "each", label: "each" }],
};

export const MEASUREMENT_FAMILIES: MeasurementFamily[] = ["WEIGHT", "VOLUME", "COUNT"];

// Reverse lookup: given a unit code already in use on a line (e.g. a carried-forward
// archived Ingredient's existing revision line, whose own measurement_family isn't part
// of the RecipeRevisionIngredientResponse shape), determine its family directly from the
// unit itself — every unit belongs to exactly one family, so this is always unambiguous.
export const FAMILY_BY_UNIT: Record<string, MeasurementFamily> = Object.fromEntries(
  (Object.entries(UNIT_OPTIONS_BY_FAMILY) as [MeasurementFamily, { value: string }[]][]).flatMap(
    ([family, units]) => units.map((unit) => [unit.value, family]),
  ),
);
