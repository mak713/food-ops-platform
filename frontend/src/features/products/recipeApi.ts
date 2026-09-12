// Typed calls to the Phase 4 Recipe/RecipeRevision endpoints, nested under Product
// (backend/app/api/v1/products.py). Mirrors backend/app/schemas/recipe.py. A
// RecipeRevision is an immutable content snapshot — creating one always submits the
// *complete* content (never a partial PATCH), matching the backend's own contract.

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";

export interface RecipeRevisionIngredient {
  id: string;
  ingredient_id: string;
  ingredient_name: string;
  ingredient_is_active: boolean;
  quantity: string;
  unit: string;
}

export interface RecipeRevision {
  id: string;
  recipe_id: string;
  revision_number: number;
  yield_quantity: string;
  active_time_minutes: number;
  elapsed_time_minutes: number | null;
  notes: string | null;
  is_current: boolean;
  ingredients: RecipeRevisionIngredient[];
}

export interface RecipeRevisionSummary {
  id: string;
  revision_number: number;
  yield_quantity: string;
  is_current: boolean;
}

export interface Recipe {
  id: string;
  product_id: string;
  name: string;
  current_revision: RecipeRevision;
}

export interface RecipeRevisionIngredientLineInput {
  ingredient_id: string;
  quantity: string;
  unit: string;
}

export interface RecipeContentInput {
  yield_quantity: string;
  active_time_minutes: number;
  elapsed_time_minutes?: number | null;
  notes?: string | null;
  ingredients: RecipeRevisionIngredientLineInput[];
}

export interface RecipeCreateRequest extends RecipeContentInput {
  name: string;
}

export interface RecipeRevisionCreateRequest extends RecipeContentInput {
  expected_current_revision_id: string;
}

export interface RecipeRenameRequest {
  name: string;
}

export interface RecipeRevisionListParams {
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

export const recipeApi = {
  get: (productId: string) => apiFetch<Recipe>(`/api/v1/products/${productId}/recipe`),
  create: (productId: string, payload: RecipeCreateRequest) =>
    post<Recipe>(`/api/v1/products/${productId}/recipe`, payload),
  rename: (productId: string, payload: RecipeRenameRequest) =>
    patch<Recipe>(`/api/v1/products/${productId}/recipe`, payload),
  listRevisions: (productId: string, params: RecipeRevisionListParams = {}) =>
    apiFetch<PageResponse<RecipeRevisionSummary>>(
      `/api/v1/products/${productId}/recipe/revisions${buildQuery(params)}`,
    ),
  getRevision: (productId: string, revisionId: string) =>
    apiFetch<RecipeRevision>(`/api/v1/products/${productId}/recipe/revisions/${revisionId}`),
  createRevision: (productId: string, payload: RecipeRevisionCreateRequest) =>
    post<RecipeRevision>(`/api/v1/products/${productId}/recipe/revisions`, payload),
};
