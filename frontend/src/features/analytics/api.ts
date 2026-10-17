// Typed calls to the Phase 10 Analytics endpoints (backend/app/api/v1/analytics.py).
// Mirrors backend/app/schemas/analytics.py. Everything shown is derived server-side from
// immutable historical Order and cost data; the client only renders it (Spec §12.5, §13).
// Money is a 2-dp Decimal string, ratios are 6-dp Decimal strings (0.125000 = 12.5%).

import { apiFetch } from "../../api/client";

export type AnalyticsRange =
  "LAST_7_DAYS" | "LAST_30_DAYS" | "LAST_90_DAYS" | "THIS_MONTH" | "THIS_YEAR" | "CUSTOM";

export const RANGE_LABELS: Record<AnalyticsRange, string> = {
  LAST_7_DAYS: "Last 7 days",
  LAST_30_DAYS: "Last 30 days",
  LAST_90_DAYS: "Last 90 days",
  THIS_MONTH: "This month",
  THIS_YEAR: "This year",
  CUSTOM: "Custom range",
};

export const PRESET_RANGES: AnalyticsRange[] = [
  "LAST_7_DAYS",
  "LAST_30_DAYS",
  "LAST_90_DAYS",
  "THIS_MONTH",
  "THIS_YEAR",
];

/** The one global period selection shared by every Analytics tab. */
export interface PeriodSelection {
  range: AnalyticsRange;
  startDate?: string;
  endDate?: string;
}

export interface Period {
  range: AnalyticsRange;
  start_date: string;
  end_date: string;
  days: number;
  lower_bound_utc: string;
  upper_bound_utc: string;
}

export interface ReportHeader {
  generated_at: string;
  business_today: string;
  timezone: string;
  period: Period;
}

export interface Kpis {
  revenue: string;
  direct_cost: string;
  contribution: string;
  contribution_margin: string | null;
  completed_orders: number;
  average_order_value: string | null;
}

export interface MoneyDelta {
  absolute: string | null;
  percent_change: string | null;
}

export interface KpiDeltas {
  revenue: MoneyDelta;
  direct_cost: MoneyDelta;
  contribution: MoneyDelta;
  contribution_margin: { absolute: string | null };
  completed_orders: { absolute: number; percent_change: string | null };
  average_order_value: MoneyDelta;
}

export interface Overview extends ReportHeader {
  current: Kpis;
  comparison_period: Period | null;
  previous: Kpis | null;
  deltas: KpiDeltas | null;
  previous_has_data: boolean | null;
}

export interface TrendBucket {
  index: number;
  label: string;
  start_date: string;
  end_date: string;
  revenue: string;
  direct_cost: string;
  contribution: string;
  completed_orders: number;
}

export interface Trends extends ReportHeader {
  granularity: "DAY" | "WEEK" | "MONTH";
  buckets: TrendBucket[];
  totals: Kpis;
}

export interface ProductOption {
  kind: "STANDARD_OPTION" | "CUSTOM_QUANTITY";
  selling_option_id: string | null;
  name: string;
  package_count: string | null;
  underlying_units: string;
  revenue: string;
  direct_cost: string;
  contribution: string;
  contribution_margin: string | null;
  completed_orders: number;
}

export interface ProductRow {
  product_id: string;
  product_name: string;
  product_type: string | null;
  is_active: boolean | null;
  underlying_units: string;
  revenue: string;
  direct_cost: string;
  contribution: string;
  contribution_margin: string | null;
  completed_orders: number;
  selling_options: ProductOption[];
}

export interface CustomItems {
  line_count: number;
  completed_orders: number;
  revenue: string;
  direct_cost: string;
  contribution: string;
  contribution_margin: string | null;
}

export interface OrderLevel {
  order_adjustments: string;
  manual_tax: string;
  unattributed_direct_cost: string;
  unreconciled_revenue: string;
}

export interface ProductHighlight {
  product_id: string;
  product_name: string;
  value: string;
}

export interface Products extends ReportHeader {
  rows: ProductRow[];
  custom_items: CustomItems | null;
  order_level: OrderLevel;
  totals: Kpis;
  most_profitable: ProductHighlight | null;
  highest_margin: ProductHighlight | null;
}

export type ProductSort = "contribution" | "margin" | "revenue" | "units" | "orders" | "name";

export interface GuestSummary {
  completed_orders: number;
  revenue: string;
  average_order_value: string | null;
  share_of_completed_orders: string | null;
}

export interface CustomerRow {
  customer_id: string;
  customer_name: string;
  is_active: boolean | null;
  classification: "NEW" | "RETURNING";
  completed_orders: number;
  revenue: string;
  average_order_value: string | null;
}

export interface Customers extends ReportHeader {
  summary: {
    unique_customers: number;
    new_customers: number;
    returning_customers: number;
    guest: GuestSummary;
  };
  items: CustomerRow[];
  total: number;
  limit: number;
  offset: number;
}

export type CustomerSort = "revenue" | "completed_orders" | "name";

export interface FulfillmentMethodRow {
  method: "PICKUP" | "DELIVERY" | "OTHER" | "UNSPECIFIED";
  completed_orders: number;
  revenue: string;
}

export interface OrderRow {
  order_id: string;
  order_number: string;
  status: "COMPLETED" | "CANCELED";
  reported_at: string;
  customer_id: string | null;
  customer_name: string | null;
  fulfillment_method: string | null;
  final_total: string;
  payments_total: string;
  payment_status: "UNPAID" | "PARTIALLY_PAID" | "PAID" | null;
  outstanding_balance: string | null;
  direct_cost: string | null;
  contribution: string | null;
}

export interface Orders extends ReportHeader {
  summary: {
    completed_orders: number;
    canceled_orders: number;
    revenue: string;
    average_order_value: string | null;
    largest_completed_order: {
      order_id: string;
      order_number: string;
      final_total: string;
      completed_at: string;
      customer_name: string | null;
    } | null;
    fulfillment_methods: FulfillmentMethodRow[];
    outstanding: { label: string; order_count: number; balance: string };
  };
  items: OrderRow[];
  total: number;
  limit: number;
  offset: number;
}

export type OrderStatusFilter = "ALL" | "COMPLETED" | "CANCELED";
export type OrderSort = "reported_at" | "final_total" | "outstanding_balance";

const BASE = "/api/v1/analytics";
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

/** Query string for the selected period only (shared by JSON and CSV so they can't drift). */
export function periodParams(selection: PeriodSelection): URLSearchParams {
  const params = new URLSearchParams();
  params.set("range", selection.range);
  if (selection.range === "CUSTOM") {
    if (selection.startDate) params.set("start_date", selection.startDate);
    if (selection.endDate) params.set("end_date", selection.endDate);
  }
  return params;
}

function withExtras(
  selection: PeriodSelection,
  extras: Record<string, string | number | boolean | undefined>,
): string {
  const params = periodParams(selection);
  for (const [key, value] of Object.entries(extras)) {
    if (value !== undefined) params.set(key, String(value));
  }
  return params.toString();
}

export interface ProductQuery {
  sort: ProductSort;
}
export interface CustomerQuery {
  sort: CustomerSort;
  limit: number;
  offset: number;
}
export interface OrderQuery {
  status: OrderStatusFilter;
  outstandingOnly: boolean;
  sort: OrderSort;
  limit: number;
  offset: number;
}

export const analyticsApi = {
  overview: (selection: PeriodSelection, compare: boolean) =>
    apiFetch<Overview>(`${BASE}/overview?${withExtras(selection, { compare })}`),
  trends: (selection: PeriodSelection) =>
    apiFetch<Trends>(`${BASE}/trends?${withExtras(selection, {})}`),
  products: (selection: PeriodSelection, q: ProductQuery) =>
    apiFetch<Products>(`${BASE}/products?${withExtras(selection, { sort: q.sort })}`),
  customers: (selection: PeriodSelection, q: CustomerQuery) =>
    apiFetch<Customers>(
      `${BASE}/customers?${withExtras(selection, { sort: q.sort, limit: q.limit, offset: q.offset })}`,
    ),
  orders: (selection: PeriodSelection, q: OrderQuery) =>
    apiFetch<Orders>(
      `${BASE}/orders?${withExtras(selection, {
        status: q.status,
        outstanding_only: q.outstandingOnly ? true : undefined,
        sort: q.sort,
        limit: q.limit,
        offset: q.offset,
      })}`,
    ),
};

/** Same-origin CSV download URLs (cookie session, plain GET — no CSRF). Each uses exactly
 * the period and table parameters of the on-screen report it exports. */
export const csvUrls = {
  trends: (selection: PeriodSelection) =>
    `${API_BASE_URL}${BASE}/trends.csv?${withExtras(selection, {})}`,
  products: (selection: PeriodSelection, q: ProductQuery) =>
    `${API_BASE_URL}${BASE}/products.csv?${withExtras(selection, { sort: q.sort })}`,
  customers: (selection: PeriodSelection, q: Pick<CustomerQuery, "sort">) =>
    `${API_BASE_URL}${BASE}/customers.csv?${withExtras(selection, { sort: q.sort })}`,
  orders: (
    selection: PeriodSelection,
    q: Pick<OrderQuery, "status" | "outstandingOnly" | "sort">,
  ) =>
    `${API_BASE_URL}${BASE}/orders.csv?${withExtras(selection, {
      status: q.status,
      outstanding_only: q.outstandingOnly ? true : undefined,
      sort: q.sort,
    })}`,
};
