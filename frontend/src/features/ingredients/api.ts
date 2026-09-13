// Typed calls to the Phase 4/5 Ingredient endpoints (backend/app/api/v1/ingredients.py).
// Mirrors backend/app/schemas/ingredient.py's request/response shapes (established
// hand-maintained-mirror convention — see features/customers/api.ts). Physical-inventory
// mutation endpoints (initial balance/restock/adjustment/replacement-cost/history) are
// Phase 5's own `inventoryApi.ts`, not this file — this file only grew the four inventory
// *display* fields Phase 4 deliberately excluded from the response.

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";

export type MeasurementFamily = "WEIGHT" | "VOLUME" | "COUNT";

export interface Ingredient {
  id: string;
  name: string;
  measurement_family: MeasurementFamily;
  canonical_unit: string;
  is_active: boolean;
  version: number;
  physical_quantity: string;
  weighted_average_unit_cost: string;
  latest_purchase_unit_cost: string | null;
  replacement_unit_cost: string | null;
  effective_replacement_cost: string | null;
}

// The list/summary shape carries only Physical Quantity, not the three cost fields
// (mirrors backend/app/schemas/ingredient.py::IngredientSummary exactly).
export interface IngredientSummary {
  id: string;
  name: string;
  measurement_family: MeasurementFamily;
  canonical_unit: string;
  is_active: boolean;
  version: number;
  physical_quantity: string;
}

export interface IngredientCreateRequest {
  name: string;
  measurement_family: MeasurementFamily;
  canonical_unit: string;
}

export interface IngredientUpdateRequest {
  version: number;
  name?: string;
}

export interface IngredientListParams {
  q?: string;
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

function buildQuery(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "POST", body: JSON.stringify(body) });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}

export const ingredientsApi = {
  list: (params: IngredientListParams = {}) =>
    apiFetch<PageResponse<IngredientSummary>>(`/api/v1/ingredients${buildQuery(params)}`),
  get: (id: string) => apiFetch<Ingredient>(`/api/v1/ingredients/${id}`),
  create: (payload: IngredientCreateRequest) => post<Ingredient>("/api/v1/ingredients", payload),
  update: (id: string, payload: IngredientUpdateRequest) =>
    patch<Ingredient>(`/api/v1/ingredients/${id}`, payload),
  archive: (id: string, version: number) =>
    post<Ingredient>(`/api/v1/ingredients/${id}/archive`, { version }),
  reactivate: (id: string, version: number) =>
    post<Ingredient>(`/api/v1/ingredients/${id}/reactivate`, { version }),
  remove: (id: string, version: number) =>
    apiFetch<void>(`/api/v1/ingredients/${id}?version=${version}`, { method: "DELETE" }),
  /** Fetches every active Ingredient across all pages of the existing paginated list
   * endpoint (Phase 4 Plan v4 §13) — no new/bulk endpoint. Used by the Recipe editor's
   * ingredient-add selector, which must offer every eligible choice, not just page one. */
  listAllActive: async (): Promise<IngredientSummary[]> => {
    const all: IngredientSummary[] = [];
    let offset = 0;
    const limit = 200; // the endpoint's own existing upper bound (Query(le=200))
    for (;;) {
      const page = await ingredientsApi.list({ is_active: true, limit, offset });
      all.push(...page.items);
      if (all.length >= page.total || page.items.length < limit) break;
      offset += limit;
    }
    return all;
  },
  /** Every Ingredient regardless of active state — the Inventory History picker must
   * still reach an archived Ingredient's history (Phase 5 Plan §F), unlike the Recipe
   * selector's active-only `listAllActive` above. */
  listAll: async (): Promise<IngredientSummary[]> => {
    const all: IngredientSummary[] = [];
    let offset = 0;
    const limit = 200;
    for (;;) {
      const page = await ingredientsApi.list({ limit, offset });
      all.push(...page.items);
      if (all.length >= page.total || page.items.length < limit) break;
      offset += limit;
    }
    return all;
  },
};
