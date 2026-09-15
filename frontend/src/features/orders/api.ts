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
}

export interface OrderListParams {
  q?: string;
  status?: OrderStatus;
  customer_id?: string;
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
};
