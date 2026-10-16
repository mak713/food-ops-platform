// Typed calls to the Phase 8 Surplus endpoints (backend/app/api/v1/surplus.py).
// Mirrors backend/app/schemas/surplus.py.

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";
import type { ManualAdjustmentReason } from "../ingredients/inventoryApi";

export interface SurplusInventory {
  id: string;
  product_id: string;
  product_name: string;
  source_production_run_id: string | null;
  physical_quantity: string;
  // Phase 9 (Spec §7.3, SUR-003): derived by the backend — `allocated` is the sum of active
  // allocations (current promises, not consumption); `available = physical − allocated`.
  allocated_quantity: string;
  available_quantity: string;
  unit_cost_basis: string;
  source_total_cost_basis: string | null;
  produced_at: string;
  usable_through_date: string | null;
  is_reusable: boolean;
  notes: string | null;
  version: number;
}

export interface SurplusTransaction {
  id: string;
  surplus_inventory_id: string;
  transaction_type: "PRODUCTION_OUTPUT" | "ORDER_FULFILLMENT" | "MANUAL_ADJUSTMENT";
  quantity_change: string;
  reason: string | null;
  notes: string | null;
  order_id: string | null;
  production_run_id: string | null;
  created_at: string;
}

export interface SurplusManualCreateRequest {
  product_id: string;
  quantity: string;
  unit_cost_basis: string;
  produced_at?: string | null;
  usable_through_date?: string | null;
  is_reusable?: boolean | null;
  notes?: string | null;
}

export interface SurplusAdjustmentRequest {
  version: number;
  // Must be negative — reduction-only; new supply always uses manual creation.
  quantity_delta: string;
  reason: ManualAdjustmentReason;
  notes?: string | null;
  acknowledged_warning_fingerprint?: string | null;
}

export interface ListParams {
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

export const surplusApi = {
  list: (params: ListParams = {}) =>
    apiFetch<PageResponse<SurplusInventory>>(`/api/v1/surplus${buildQuery(params)}`),
  get: (surplusId: string) => apiFetch<SurplusInventory>(`/api/v1/surplus/${surplusId}`),
  create: (payload: SurplusManualCreateRequest) =>
    post<SurplusInventory>("/api/v1/surplus", payload),
  adjust: (surplusId: string, payload: SurplusAdjustmentRequest) =>
    post<SurplusInventory>(`/api/v1/surplus/${surplusId}/adjust`, payload),
  listTransactions: (surplusId: string, params: ListParams = {}) =>
    apiFetch<PageResponse<SurplusTransaction>>(
      `/api/v1/surplus/${surplusId}/transactions${buildQuery(params)}`,
    ),
};
