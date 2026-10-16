// Typed call to the Phase 9 Dashboard endpoint (backend/app/api/v1/dashboard.py).
// Mirrors backend/app/schemas/dashboard.py. Everything is derived server-side from
// authoritative operational state on every read (DASH-005); the client only renders it.

import { apiFetch } from "../../api/client";

export interface DashboardAlertItem {
  label: string;
  detail: string | null;
  path: string | null;
}

export interface DashboardAlert {
  code: string;
  severity: "WARNING" | "NOTICE";
  title: string;
  condition: string;
  impact: string;
  count: number;
  destination_label: string;
  destination_path: string;
  items: DashboardAlertItem[];
  items_truncated: boolean;
}

export interface ActiveRun {
  run_id: string;
  product_id: string;
  product_name: string;
  planned_batches: number;
  planned_output_quantity: string;
  started_at: string;
  elapsed_minutes: number;
  expected_elapsed_minutes: number | null;
  expected_finish_at: string | null;
  is_past_expected: boolean;
}

export type ProductionTiming = "DUE_TODAY" | "ADVANCE_PREP" | "START_OVERDUE";

export interface ProductionItem {
  requirement_id: string;
  product_id: string;
  product_name: string;
  demand_date: string;
  timing: ProductionTiming;
  production_demand_quantity: string;
  recommended_batches: number | null;
  expected_output_quantity: string | null;
  estimated_active_minutes: number | null;
  suggested_start_at: string | null;
  has_shortage: boolean;
  affected_order_count: number;
}

export interface ManualWorkload {
  total_minutes: number;
  line_count: number;
  order_count: number;
}

export interface UnsatisfiedLine {
  line_id: string;
  display_name: string;
  reason:
    | "PRODUCTION_PENDING"
    | "PURCHASED_STOCK_SHORT"
    | "MANUAL_CONFIRMATION_REQUIRED"
    | "PRODUCT_UNAVAILABLE";
}

export interface FulfillmentItem {
  order_id: string;
  order_number: string;
  customer_name: string | null;
  status: "CONFIRMED" | "READY";
  fulfillment_time: string | null;
  fulfillment_method: string | null;
  final_total: string;
  payment_status: "UNPAID" | "PARTIALLY_PAID" | "PAID";
  timing: "ON_TRACK" | "AT_RISK" | "OVERDUE";
  unsatisfied_lines: UnsatisfiedLine[];
}

export interface Snapshot {
  orders_due_today: number;
  orders_ready: number;
  orders_awaiting: number;
  orders_at_risk: number;
  orders_overdue: number;
  scheduled_order_value_today: string;
  production_items_today: number;
  planned_batches_today: number;
  active_runs: number;
}

export interface UpcomingOrder {
  order_id: string;
  order_number: string;
  customer_name: string | null;
  status: string;
  fulfillment_time: string | null;
}

export interface UpcomingDay {
  date: string;
  order_count: number;
  scheduled_order_value: string;
  orders: UpcomingOrder[];
  orders_truncated: boolean;
  production_item_count: number;
  planned_batches: number;
}

export interface Dashboard {
  generated_at: string;
  business_today: string;
  timezone: string;
  attention: DashboardAlert[];
  active_production: ActiveRun[];
  todays_production: { items: ProductionItem[]; manual_workload: ManualWorkload | null };
  todays_fulfillments: FulfillmentItem[];
  todays_snapshot: Snapshot;
  upcoming: UpcomingDay[];
}

export const dashboardApi = {
  get: () => apiFetch<Dashboard>("/api/v1/dashboard"),
};
