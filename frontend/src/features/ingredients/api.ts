// Typed calls to the Phase 4 Ingredient endpoints (backend/app/api/v1/ingredients.py).
// Mirrors backend/app/schemas/ingredient.py's request/response shapes (established
// hand-maintained-mirror convention — see features/customers/api.ts). No cost/quantity
// fields anywhere — Phase 5 scope (Phase 4 Plan v4 §4/§10).

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
}

export type IngredientSummary = Ingredient;

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
};
