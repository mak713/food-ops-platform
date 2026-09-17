// Typed calls to the Phase 6 Order/Payment endpoints (backend/app/api/v1/orders.py).
// Mirrors backend/app/schemas/order.py. Money/quantity fields are kept as strings,
// matching the established wire-format convention (see features/customers/api.ts).

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";

export type OrderStatus = "DRAFT" | "CONFIRMED" | "READY" | "COMPLETED" | "CANCELED";
export type OrderLineType = "STANDARD_OPTION" | "CUSTOM_QUANTITY" | "CUSTOM_ITEM";
export type FulfillmentMethod = "PICKUP" | "DELIVERY" | "OTHER";
export type PaymentStatus = "UNPAID" | "PARTIALLY_PAID" | "PAID";

export interface ConfirmationIssue {
  severity: string;
  code: string;
  message: string;
  field: string | null;
}

export interface OrderLine {
  id: string;
  line_type: OrderLineType;
  product_id: string | null;
  selling_option_id: string | null;
  display_name_snapshot: string;
  package_quantity: string;
  underlying_quantity: string;
  charged_unit_price_snapshot: string;
  line_subtotal: string;
  packaging_cost_per_package_snapshot: string;
  packaging_cost_total_snapshot: string;
  price_override_reason: string | null;
  custom_direct_cost_estimate: string | null;
  custom_active_time_minutes: number | null;
  manual_fulfillment_required: boolean;
  manual_fulfillment_satisfied: boolean;
  notes: string | null;
}

export interface Payment {
  id: string;
  amount: string;
  payment_method: string;
  payment_date: string;
  notes: string | null;
}

export interface Order {
  id: string;
  order_number: string;
  status: OrderStatus;
  customer_id: string | null;
  fulfillment_date: string | null;
  fulfillment_time: string | null;
  fulfillment_method: FulfillmentMethod | null;
  fulfillment_details: string | null;
  fulfillment_notes: string | null;
  internal_notes: string | null;
  subtotal: string;
  order_adjustment: string;
  adjustment_description: string | null;
  manual_tax: string;
  final_total: string;
  estimated_direct_cost: string | null;
  estimated_contribution: string | null;
  estimated_contribution_margin: string | null;
  version: number;
  lines: OrderLine[];
  payments: Payment[];
  payments_total: string;
  payment_status: PaymentStatus;
  overpayment_amount: string | null;
  is_confirmable: boolean;
  confirmation_issues: ConfirmationIssue[];
  // Phase 7: derived, non-persisted, read-time-only — advisory for UI gating.
  // Never trusted for correctness; every write path re-checks authoritatively.
  production_locked: boolean;
}

export interface OrderSummary {
  id: string;
  order_number: string;
  status: OrderStatus;
  customer_id: string | null;
  customer_name: string | null;
  fulfillment_date: string | null;
  final_total: string;
  payment_status: PaymentStatus;
  version: number;
}

// `id: undefined` (a new, not-yet-saved line) vs. `id: string` (a retained, persisted
// line) — never RHF's own internal field-array key (see OrderEntryPage's `serverId`
// convention, kept as a distinct form-state property never sent to the API directly;
// the submit mapper produces this shape from that distinct property).
export interface OrderLineInput {
  id?: string | null;
  line_type: OrderLineType;
  product_id?: string | null;
  selling_option_id?: string | null;
  package_quantity?: string | null;
  underlying_quantity?: string | null;
  charged_unit_price?: string | null;
  price_override_reason?: string | null;
  packaging_cost_per_package?: string | null;
  display_name?: string | null;
  custom_direct_cost_estimate?: string | null;
  custom_active_time_minutes?: number | null;
  manual_fulfillment_required?: boolean | null;
  notes?: string | null;
}

export interface PaymentInput {
  amount: string;
  payment_method: string;
  payment_date: string;
  notes?: string | null;
  confirm_overpayment?: boolean;
}

interface OrderHeaderInput {
  customer_id?: string | null;
  fulfillment_date?: string | null;
  fulfillment_time?: string | null;
  fulfillment_method?: FulfillmentMethod | null;
  fulfillment_details?: string | null;
  fulfillment_notes?: string | null;
  internal_notes?: string | null;
  order_adjustment?: string;
  adjustment_description?: string | null;
  manual_tax?: string;
  lines: OrderLineInput[];
}

export interface OrderCreateRequest extends OrderHeaderInput {
  payment?: PaymentInput | null;
}

export interface OrderUpdateRequest extends OrderHeaderInput {
  version: number;
  confirm_overpayment?: boolean;
  // Phase 7 (Implementation Remediation Plan, Finding 1) — only meaningful (and
  // only required) when editing a CONFIRMED order via the same PATCH endpoint: the
  // exact fingerprint set reviewed on the preceding Preview call. Harmlessly
  // ignored by the backend for a DRAFT edit.
  acknowledged_warning_fingerprints?: string[];
}

export interface OrderListParams {
  q?: string;
  status?: OrderStatus;
  customer_id?: string;
  limit?: number;
  offset?: number;
}

// --- Phase 7: Confirm/Cancel/Preview ---------------------------------------------

export interface OrderConfirmRequest {
  version: number;
  acknowledged_warning_fingerprints: string[];
}

export interface OrderCancelRequest {
  version: number;
}

export interface OperationalWarning {
  severity: string;
  code: string;
  message: string;
  field: string | null;
  resource: string;
  details: Record<string, unknown>;
}

// Phase 7 Implementation Remediation Plan, Finding 2 (amendment 2): every group
// below distinguishes the existing authoritative BASELINE (excluding the Order
// being previewed) from the PROJECTED "after" picture and the server-computed
// INCREMENTAL delta — never reconstructed client-side.
export interface ProductionRequirementPreviewItem {
  product_id: string;
  recipe_revision_id: string | null;
  demand_date: string;
  is_protected: boolean;
  missing_recipe: boolean;
  baseline_confirmed_demand_quantity: string;
  baseline_surplus_allocated_quantity: string;
  baseline_production_demand_quantity: string;
  baseline_recommended_batches: number | null;
  projected_confirmed_demand_quantity: string;
  projected_surplus_allocated_quantity: string;
  projected_production_demand_quantity: string;
  projected_recommended_batches: number | null;
  projected_expected_output_quantity: string | null;
  projected_expected_excess_quantity: string | null;
  projected_estimated_active_minutes: number | null;
  projected_estimated_elapsed_minutes: number | null;
  projected_estimated_ingredient_cost: string | null;
  projected_estimated_labor_cost: string | null;
  projected_estimated_direct_production_cost: string | null;
  projected_suggested_start_at: string | null;
  incremental_confirmed_demand_quantity: string;
  incremental_production_demand_quantity: string;
}

export interface IngredientAvailabilityPreviewItem {
  ingredient_id: string;
  ingredient_name: string;
  canonical_unit: string;
  physical_quantity: string;
  baseline_shortage_quantity: string;
  projected_shortage_quantity: string;
}

export interface PurchasedShortagePreviewItem {
  product_id: string;
  baseline_shortage_quantity: string;
  projected_shortage_quantity: string;
}

export interface CustomItemWorkloadPreviewItem {
  demand_date: string;
  baseline_total_active_minutes: number;
  projected_total_active_minutes: number;
  baseline_contributing_line_count: number;
  projected_contributing_line_count: number;
}

export interface OperationalPreviewResponse {
  subtotal: string;
  final_total: string;
  warnings: OperationalWarning[];
  warning_fingerprints: string[];
  production_requirements: ProductionRequirementPreviewItem[];
  ingredient_availability: IngredientAvailabilityPreviewItem[];
  purchased_shortages: PurchasedShortagePreviewItem[];
  custom_item_workload: CustomItemWorkloadPreviewItem[];
  fulfillment_date_required_for_operational_preview: boolean;
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

export const ordersApi = {
  list: (params: OrderListParams = {}) =>
    apiFetch<PageResponse<OrderSummary>>(`/api/v1/orders${buildQuery(params)}`),
  get: (id: string) => apiFetch<Order>(`/api/v1/orders/${id}`),
  create: (payload: OrderCreateRequest) => post<Order>("/api/v1/orders", payload),
  update: (id: string, payload: OrderUpdateRequest) =>
    patch<Order>(`/api/v1/orders/${id}`, payload),
  remove: (id: string, version: number, confirmDeleteWithPayments = false) =>
    apiFetch<void>(
      `/api/v1/orders/${id}?version=${version}&confirm_delete_with_payments=${confirmDeleteWithPayments}`,
      { method: "DELETE" },
    ),
  addPayment: (id: string, payload: PaymentInput) =>
    post<Order>(`/api/v1/orders/${id}/payments`, payload),
  confirm: (id: string, payload: OrderConfirmRequest) =>
    post<Order>(`/api/v1/orders/${id}/confirm`, payload),
  cancel: (id: string, payload: OrderCancelRequest) =>
    post<Order>(`/api/v1/orders/${id}/cancel`, payload),
  preview: (payload: OrderCreateRequest) =>
    post<OperationalPreviewResponse>("/api/v1/orders/preview", payload),
  previewExisting: (id: string, payload: OrderCreateRequest) =>
    post<OperationalPreviewResponse>(`/api/v1/orders/${id}/preview`, payload),
};
