// Typed calls to the Phase 5 Ingredient physical-inventory endpoints
// (backend/app/api/v1/ingredient_inventory.py). Mirrors backend/app/schemas/inventory.py.

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";
import type { Ingredient } from "./api";

export type ManualAdjustmentReason =
  | "COUNT_CORRECTION"
  | "SPOILAGE_OR_WASTE"
  | "PERSONAL_OR_INTERNAL_USE"
  | "DAMAGE"
  | "OTHER";

export interface InventoryTransaction {
  id: string;
  transaction_type: string;
  quantity_change: string;
  unit_cost: string | null;
  total_cost: string | null;
  supplier_text: string | null;
  reason: string | null;
  notes: string | null;
  created_at: string;
}

export interface IngredientInitialBalanceRequest {
  version: number;
  quantity: string;
  unit: string;
  unit_cost: string;
  supplier_text?: string | null;
  notes?: string | null;
}

export type IngredientRestockRequest = IngredientInitialBalanceRequest;

export interface IngredientAdjustmentRequest {
  version: number;
  quantity_change: string;
  reason: ManualAdjustmentReason;
  notes?: string | null;
}

export interface ReplacementCostRequest {
  version: number;
  replacement_unit_cost: string | null;
}

export interface TransactionListParams {
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

function put<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "PUT", body: JSON.stringify(body) });
}

export const ingredientInventoryApi = {
  createInitialBalance: (ingredientId: string, payload: IngredientInitialBalanceRequest) =>
    post<Ingredient>(`/api/v1/ingredients/${ingredientId}/inventory/initial-balance`, payload),
  restock: (ingredientId: string, payload: IngredientRestockRequest) =>
    post<Ingredient>(`/api/v1/ingredients/${ingredientId}/inventory/restock`, payload),
  adjust: (ingredientId: string, payload: IngredientAdjustmentRequest) =>
    post<Ingredient>(`/api/v1/ingredients/${ingredientId}/inventory/adjustments`, payload),
  setReplacementCost: (ingredientId: string, payload: ReplacementCostRequest) =>
    put<Ingredient>(`/api/v1/ingredients/${ingredientId}/inventory/replacement-cost`, payload),
  listTransactions: (ingredientId: string, params: TransactionListParams = {}) =>
    apiFetch<PageResponse<InventoryTransaction>>(
      `/api/v1/ingredients/${ingredientId}/inventory/transactions${buildQuery(params)}`,
    ),
};
