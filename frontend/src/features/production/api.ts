// Typed calls to the Phase 8 Production Run endpoints
// (backend/app/api/v1/production_runs.py). Mirrors backend/app/schemas/production.py.

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";

export type ProductionRunStatus = "IN_PRODUCTION" | "COMPLETED" | "CANCELED";
export type CostBasisSource = "WAC" | "FALLBACK";

export interface ProductionRequirement {
  id: string;
  product_id: string;
  product_name: string;
  recipe_revision_id: string | null;
  demand_date: string;
  calculation_status: string;
  confirmed_demand_quantity: string;
  surplus_allocated_quantity: string;
  production_demand_quantity: string;
  recommended_batches: number | null;
  expected_output_quantity: string | null;
  expected_excess_quantity: string | null;
  estimated_active_minutes: number | null;
  estimated_elapsed_minutes: number | null;
  suggested_start_at: string | null;
  estimated_ingredient_cost: string | null;
  estimated_labor_cost: string | null;
  estimated_direct_production_cost: string | null;
}

export interface ProductionRunIngredient {
  ingredient_id: string;
  ingredient_name: string;
  planned_quantity: string;
  actual_quantity: string | null;
  weighted_average_unit_cost_snapshot: string;
  replacement_unit_cost_snapshot: string | null;
  cost_basis_source: CostBasisSource;
  actual_unit_cost_basis: string | null;
  actual_total_cost: string | null;
}

export interface ProductionRun {
  id: string;
  business_id: string;
  source_production_requirement_id: string | null;
  product_id: string;
  product_name: string;
  recipe_revision_id: string;
  status: ProductionRunStatus;
  planned_batches: number;
  planned_output_quantity: string;
  actual_output_quantity: string | null;
  recipe_yield_snapshot: string;
  active_minutes_per_batch_snapshot: number;
  elapsed_minutes_per_batch_snapshot: number | null;
  labor_rate_snapshot: string;
  estimated_ingredient_cost: string;
  estimated_labor_cost: string;
  estimated_total_production_cost: string;
  actual_ingredient_cost: string | null;
  actual_labor_cost: string | null;
  actual_total_production_cost: string | null;
  started_at: string;
  completed_at: string | null;
  canceled_at: string | null;
  version: number;
  run_ingredients: ProductionRunIngredient[];
}

export interface ProductionStartRequest {
  production_requirement_id: string;
  batches?: number | null;
  acknowledged_warning_fingerprint?: string | null;
}

export interface ProductionFinishRequest {
  version: number;
  actual_output_quantity: string;
  ingredient_actual_quantities?: Record<string, string> | null;
}

export interface ProductionFinishResponse {
  run: ProductionRun;
  negative_inventory_ingredient_ids: string[];
  is_shortfall: boolean;
}

export interface ProductionCancelRequest {
  version: number;
  reason?: string | null;
}

export interface ListParams {
  limit?: number;
  offset?: number;
}

export interface ListRunsParams extends ListParams {
  status?: ProductionRunStatus;
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

export const productionApi = {
  listRequirements: (params: ListParams = {}) =>
    apiFetch<PageResponse<ProductionRequirement>>(
      `/api/v1/production-runs/requirements${buildQuery(params)}`,
    ),
  getRequirement: (requirementId: string) =>
    apiFetch<ProductionRequirement>(`/api/v1/production-runs/requirements/${requirementId}`),
  listRuns: (params: ListRunsParams = {}) =>
    apiFetch<PageResponse<ProductionRun>>(`/api/v1/production-runs${buildQuery(params)}`),
  get: (runId: string) => apiFetch<ProductionRun>(`/api/v1/production-runs/${runId}`),
  start: (payload: ProductionStartRequest) =>
    post<ProductionRun>("/api/v1/production-runs", payload),
  finish: (runId: string, payload: ProductionFinishRequest) =>
    post<ProductionFinishResponse>(`/api/v1/production-runs/${runId}/finish`, payload),
  cancel: (runId: string, payload: ProductionCancelRequest) =>
    post<ProductionRun>(`/api/v1/production-runs/${runId}/cancel`, payload),
};
