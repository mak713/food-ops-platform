// Phase 9 Dashboard behavior tests (Spec §12.15, DASH-001..005; Acceptance Matrix F1/F3/F4):
// sections render in priority order, empty sections are hidden, alerts show condition +
// impact + a destination, status is conveyed with text, and the query always refetches on
// mount so the page reflects writes made elsewhere. No business math is performed here —
// every figure is rendered from the server payload.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";
import type { Dashboard } from "../../src/features/dashboard/api";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ME = () =>
  jsonResponse(200, {
    user: { id: "u1", name: "Test Owner", email: "owner@example.com" },
    business: { id: "b1", name: "Test Bakery", timezone: "America/New_York" },
  });

function stubFetch(dashboard: () => Response) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.endsWith("/api/v1/auth/me")) {
      return Promise.resolve(ME());
    }
    if (url.endsWith("/api/v1/dashboard")) {
      return Promise.resolve(dashboard());
    }
    throw new Error(`Unhandled fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderDashboard(queryClient?: QueryClient) {
  const router = createMemoryRouter(routes, { initialEntries: ["/app/dashboard"] });
  const client = queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { router, client };
}

const EMPTY: Dashboard = {
  generated_at: "2026-06-10T12:00:00Z",
  business_today: "2026-06-10",
  timezone: "America/New_York",
  attention: [],
  active_production: [],
  todays_production: { items: [], manual_workload: null },
  todays_fulfillments: [],
  todays_snapshot: {
    orders_due_today: 0,
    orders_ready: 0,
    orders_awaiting: 0,
    orders_at_risk: 0,
    orders_overdue: 0,
    scheduled_order_value_today: "0.00",
    production_items_today: 0,
    planned_batches_today: 0,
    active_runs: 0,
  },
  upcoming: [],
};

const FULL: Dashboard = {
  ...EMPTY,
  attention: [
    {
      code: "INGREDIENT_SHORTAGE",
      severity: "WARNING",
      title: "2 ingredients are short for upcoming demand",
      condition: "Reserved demand within the shopping horizon exceeds the physical stock on hand.",
      impact: "Production for the affected orders cannot be fully supplied without restocking.",
      count: 7,
      destination_label: "Open Shopping List",
      destination_path: "/app/production/shopping-list",
      items: [{ label: "Flour", detail: "short 90 g (needed by 2026-06-10)", path: null }],
      items_truncated: true,
    },
    {
      code: "SURPLUS_EXPIRING",
      severity: "NOTICE",
      title: "1 surplus lot is nearing its usable-through date",
      condition: "Available surplus will pass its seller-defined usable-through date soon.",
      impact: "Unused surplus will no longer be eligible for new demand after that date.",
      count: 1,
      destination_label: "Review surplus",
      destination_path: "/app/surplus",
      items: [],
      items_truncated: false,
    },
  ],
  active_production: [
    {
      run_id: "run1",
      product_id: "p1",
      product_name: "Sourdough Loaf",
      planned_batches: 2,
      planned_output_quantity: "24.000000",
      started_at: "2026-06-10T10:00:00Z",
      elapsed_minutes: 150,
      expected_elapsed_minutes: 120,
      expected_finish_at: "2026-06-10T12:00:00Z",
      is_past_expected: true,
    },
  ],
  todays_production: {
    items: [
      {
        requirement_id: "req1",
        product_id: "p2",
        product_name: "Cinnamon Rolls",
        demand_date: "2026-06-11",
        timing: "ADVANCE_PREP",
        production_demand_quantity: "12.000000",
        recommended_batches: 1,
        expected_output_quantity: "12.000000",
        estimated_active_minutes: 45,
        suggested_start_at: "2026-06-10T14:00:00Z",
        has_shortage: true,
        affected_order_count: 1,
      },
    ],
    manual_workload: { total_minutes: 45, line_count: 1, order_count: 1 },
  },
  todays_fulfillments: [
    {
      order_id: "o1",
      order_number: "ORD-1",
      customer_name: null,
      status: "CONFIRMED",
      fulfillment_time: "09:30:00",
      fulfillment_method: "PICKUP",
      final_total: "1234.50",
      payment_status: "PARTIALLY_PAID",
      timing: "AT_RISK",
      unsatisfied_lines: [{ line_id: "l1", display_name: "Cookies", reason: "PRODUCTION_PENDING" }],
    },
    {
      order_id: "o2",
      order_number: "ORD-2",
      customer_name: "Alice",
      status: "READY",
      fulfillment_time: null,
      fulfillment_method: null,
      final_total: "10.00",
      payment_status: "PAID",
      timing: "ON_TRACK",
      unsatisfied_lines: [],
    },
  ],
  todays_snapshot: {
    ...EMPTY.todays_snapshot,
    orders_due_today: 2,
    orders_ready: 1,
    orders_awaiting: 1,
    orders_at_risk: 1,
    scheduled_order_value_today: "1244.50",
    production_items_today: 1,
    planned_batches_today: 1,
    active_runs: 1,
  },
  upcoming: [
    {
      date: "2026-06-11",
      order_count: 1,
      scheduled_order_value: "55.00",
      orders: [
        {
          order_id: "o3",
          order_number: "ORD-3",
          customer_name: "Bob",
          status: "CONFIRMED",
          fulfillment_time: null,
        },
      ],
      orders_truncated: false,
      production_item_count: 1,
      planned_batches: 1,
    },
  ],
};

describe("Dashboard page", () => {
  it("shows a loading state, then the populated sections in priority order", async () => {
    stubFetch(() => jsonResponse(200, FULL));
    renderDashboard();

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Attention Required" });

    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual([
      "Attention Required",
      "Active Production",
      "Today's Production",
      "Today's Fulfillments",
      "Today's Snapshot",
      "Upcoming",
    ]);
  });

  it("hides every empty section and says so once, instead of showing blank panels", async () => {
    stubFetch(() => jsonResponse(200, EMPTY));
    renderDashboard();

    expect(await screen.findByTestId("dashboard-empty")).toBeInTheDocument();
    expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
  });

  it("shows only the sections that have content", async () => {
    stubFetch(() =>
      jsonResponse(200, { ...EMPTY, attention: FULL.attention.slice(1), upcoming: FULL.upcoming }),
    );
    renderDashboard();
    await screen.findByRole("heading", { name: "Attention Required" });
    expect(screen.getByRole("heading", { name: "Upcoming" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Active Production" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Today's Fulfillments" })).not.toBeInTheDocument();
  });

  it("shows each alert's condition, impact, severity as text, and an actionable destination", async () => {
    stubFetch(() => jsonResponse(200, FULL));
    renderDashboard();
    const shortage = await screen.findByTestId("alert-INGREDIENT_SHORTAGE");

    expect(within(shortage).getByText("Warning")).toBeInTheDocument();
    expect(within(shortage).getByText(/exceeds the physical stock on hand/)).toBeInTheDocument();
    expect(within(shortage).getByText(/cannot be fully supplied/)).toBeInTheDocument();
    expect(within(shortage).getByRole("link", { name: "Open Shopping List" })).toHaveAttribute(
      "href",
      "/app/production/shopping-list",
    );
    // Aggregation: the true total is reported even though only some items are listed.
    expect(within(shortage).getByText(/and 6 more/)).toBeInTheDocument();

    const notice = screen.getByTestId("alert-SURPLUS_EXPIRING");
    expect(within(notice).getByText("Notice")).toBeInTheDocument();
  });

  it("renders fulfillments with readable status, timing, payment text and the blockers", async () => {
    stubFetch(() => jsonResponse(200, FULL));
    renderDashboard();
    await screen.findByRole("heading", { name: "Today's Fulfillments" });

    const section = screen
      .getByRole("heading", { name: "Today's Fulfillments" })
      .closest("section")!;
    expect(within(section).getByText("9:30 AM")).toBeInTheDocument();
    expect(within(section).getByText("Guest")).toBeInTheDocument();
    expect(within(section).getByText("No time set")).toBeInTheDocument();
    expect(within(section).getByText("At risk")).toBeInTheDocument();
    expect(within(section).getByText("Partially paid")).toBeInTheDocument();
    expect(within(section).getByText("Paid")).toBeInTheDocument();
    expect(within(section).getByText(/Confirmed — not ready/)).toBeInTheDocument();
    expect(within(section).getByText(/waiting on production or surplus/)).toBeInTheDocument();
    expect(within(section).getByText("$1,234.50")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "ORD-1" })).toHaveAttribute(
      "href",
      "/app/orders/o1",
    );
  });

  it("labels advance prep, shortages, the no-suggested-start case, and manual workload", async () => {
    stubFetch(() => jsonResponse(200, FULL));
    renderDashboard();
    await screen.findByRole("heading", { name: "Today's Production" });
    const section = screen.getByRole("heading", { name: "Today's Production" }).closest("section")!;

    expect(within(section).getByText("Advance prep")).toBeInTheDocument();
    expect(within(section).getByText(/for Thu, Jun 11/)).toBeInTheDocument();
    expect(within(section).getByText("Ingredient shortage")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: /review & start/i })).toHaveAttribute(
      "href",
      "/app/production/start/req1",
    );
    expect(screen.getByTestId("manual-workload")).toHaveTextContent(/your own estimate/);
  });

  it("states a missing suggested start instead of inventing one", async () => {
    const item = {
      ...FULL.todays_production.items[0],
      suggested_start_at: null,
      timing: "DUE_TODAY" as const,
    };
    stubFetch(() =>
      jsonResponse(200, { ...EMPTY, todays_production: { items: [item], manual_workload: null } }),
    );
    renderDashboard();
    expect(await screen.findByText("No suggested start time")).toBeInTheDocument();
  });

  it("labels scheduled value as not revenue and shows no analytics metrics", async () => {
    stubFetch(() => jsonResponse(200, FULL));
    renderDashboard();
    await screen.findByRole("heading", { name: "Today's Snapshot" });
    expect(screen.getByText(/Scheduled order value today \(not revenue\)/)).toBeInTheDocument();
    expect(screen.getByText("$1,244.50")).toBeInTheDocument();
    expect(screen.queryByText(/contribution|direct cost|average order value|AOV/i)).toBeNull();
    expect(screen.queryByText(/^revenue$/i)).toBeNull();
  });

  it("shows the active run guidance and links to the run", async () => {
    stubFetch(() => jsonResponse(200, FULL));
    renderDashboard();
    await screen.findByRole("heading", { name: "Active Production" });
    expect(screen.getByText("Running longer than the planned time")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open run" })).toHaveAttribute(
      "href",
      "/app/production/runs/run1",
    );
  });

  it("shows an error banner with Retry when the request fails, keeping the page title", async () => {
    stubFetch(() =>
      jsonResponse(500, { error: { code: "INTERNAL", message: "boom", issues: [] } }),
    );
    renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
  });

  it("treats a malformed payload as an error rather than crashing", async () => {
    stubFetch(() => jsonResponse(200, { unexpected: true }));
    renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent(/not in the expected format/i);
  });

  it("refetches on mount (always) so it reflects writes made on other pages", async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, EMPTY));
    const { client } = renderDashboard();
    await screen.findByTestId("dashboard-empty");
    const dashboardCalls = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/v1/dashboard")).length;
    expect(dashboardCalls()).toBe(1);

    // Remount with the same (now cached) client: a fresh mount must refetch, not reuse.
    cleanup();
    const router = createMemoryRouter(routes, { initialEntries: ["/app/dashboard"] });
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <RouterProvider router={router} />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await screen.findByRole("heading", { name: "Dashboard" });
    await vi.waitFor(() => expect(dashboardCalls()).toBe(2));
  });
});
