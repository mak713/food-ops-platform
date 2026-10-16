// Typed call to the Phase 9 Shopping List endpoint (backend/app/api/v1/shopping_list.py).
// Mirrors backend/app/schemas/shopping_list.py. A read-only report: shortages are computed
// by the backend (Spec §12.5) — this module only fetches and types them.

import { apiFetch } from "../../api/client";

export interface ShoppingListDateShortage {
  demand_date: string;
  cumulative_reserved_through_date: string;
  cumulative_shortage_through_date: string;
}

export interface ShoppingListOrder {
  order_id: string;
  order_number: string;
  status: string;
  customer_name: string | null;
  fulfillment_date: string | null;
  fulfillment_time: string | null;
  demand_quantity: string;
}

export interface ShoppingListContributor {
  requirement_id: string;
  product_id: string;
  product_name: string;
  recipe_revision_id: string | null;
  demand_date: string;
  reserved_quantity: string;
  is_overdue: boolean;
  is_in_production: boolean;
  orders: ShoppingListOrder[];
}

export interface ShoppingListItem {
  ingredient_id: string;
  ingredient_name: string;
  canonical_unit: string;
  physical_quantity: string;
  reserved_through_end: string;
  shortage_quantity: string;
  needed_by_date: string | null;
  by_date: ShoppingListDateShortage[];
  contributors: ShoppingListContributor[];
}

export interface IncompleteRecipeNotice {
  requirement_id: string;
  product_id: string;
  product_name: string;
  demand_date: string;
  demand_quantity: string;
}

export interface ShoppingListRange {
  start_date: string;
  end_date: string;
  horizon_days: number | null;
  is_custom: boolean;
}

export interface ShoppingList {
  business_today: string;
  range: ShoppingListRange;
  shortages: ShoppingListItem[];
  covered: ShoppingListItem[];
  incomplete_recipe: IncompleteRecipeNotice[];
}

/** `{}` asks for the Business default preset; a preset or a custom range is explicit. */
export type ShoppingListSelection =
  | { kind: "default" }
  | { kind: "preset"; horizonDays: 3 | 7 }
  | { kind: "custom"; startDate: string; endDate: string };

export function selectionToQuery(selection: ShoppingListSelection): string {
  switch (selection.kind) {
    case "default":
      return "";
    case "preset":
      return `?horizon_days=${selection.horizonDays}`;
    case "custom":
      return `?start_date=${selection.startDate}&end_date=${selection.endDate}`;
  }
}

export const shoppingListApi = {
  get: (selection: ShoppingListSelection) =>
    apiFetch<ShoppingList>(`/api/v1/shopping-list${selectionToQuery(selection)}`),
};
