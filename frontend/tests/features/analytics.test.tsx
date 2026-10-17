// Phase 10 Analytics behavior tests (Spec §12.16; AC-ANA-*, AC-UX-004/006/007). The
// frontend never computes a business value: every figure below is rendered from the server
// payload (the numbers mirror the backend's hand-computed `Golden` scenario).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import type {
  Customers,
  Orders,
  Overview,
  Period,
  Products,
  Trends,
} from "../../src/features/analytics/api";
import { AuthProvider } from "../../src/features/auth/AuthContext";

beforeAll(() => {
  // Recharts' ResponsiveContainer needs ResizeObserver, which jsdom does not provide.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
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
    business: { id: "b1", name: "Test Bakery", timezone: "America/Chicago" },
  });

type Responder = (url: URL) => Response;

function stubFetch(respond: Responder) {
  const fetchMock = vi.fn().mockImplementation((input: string) => {
    if (input.endsWith("/api/v1/auth/me")) return Promise.resolve(ME());
    return Promise.resolve(respond(new URL(input, "http://localhost")));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return router;
}

const PERIOD: Period = {
  range: "LAST_7_DAYS",
  start_date: "2026-10-10",
  end_date: "2026-10-16",
  days: 7,
  lower_bound_utc: "2026-10-10T05:00:00Z",
  upper_bound_utc: "2026-10-17T05:00:00Z",
};
const HEADER = {
  generated_at: "2026-10-16T18:00:00Z",
  business_today: "2026-10-16",
  timezone: "America/Chicago",
  period: PERIOD,
};
const KPIS = {
  revenue: "166.00",
  direct_cost: "47.05",
  contribution: "118.95",
  contribution_margin: "0.716566",
  completed_orders: 7,
  average_order_value: "23.71",
};

const OVERVIEW_COMPARE: Overview = {
  ...HEADER,
  current: KPIS,
  comparison_period: {
    ...PERIOD,
    start_date: "2026-10-03",
    end_date: "2026-10-09",
    lower_bound_utc: "2026-10-03T05:00:00Z",
    upper_bound_utc: "2026-10-10T05:00:00Z",
  },
  previous: {
    revenue: "39.00",
    direct_cost: "11.00",
    contribution: "28.00",
    contribution_margin: "0.717949",
    completed_orders: 2,
    average_order_value: "19.50",
  },
  deltas: {
    revenue: { absolute: "127.00", percent_change: "3.256410" },
    direct_cost: { absolute: "36.05", percent_change: "3.277273" },
    contribution: { absolute: "90.95", percent_change: "3.248214" },
    contribution_margin: { absolute: "-0.001383" },
    completed_orders: { absolute: 5, percent_change: "2.500000" },
    average_order_value: { absolute: "4.21", percent_change: "0.215897" },
  },
  previous_has_data: true,
};
const OVERVIEW_PLAIN: Overview = {
  ...HEADER,
  current: KPIS,
  comparison_period: null,
  previous: null,
  deltas: null,
  previous_has_data: null,
};

const TRENDS: Trends = {
  ...HEADER,
  granularity: "DAY",
  buckets: [
    {
      index: 0,
      label: "2026-10-10",
      start_date: "2026-10-10",
      end_date: "2026-10-10",
      revenue: "4.00",
      direct_cost: "1.00",
      contribution: "3.00",
      completed_orders: 1,
    },
    {
      index: 1,
      label: "2026-10-11",
      start_date: "2026-10-11",
      end_date: "2026-10-11",
      revenue: "0.00",
      direct_cost: "0.00",
      contribution: "0.00",
      completed_orders: 0,
    },
  ],
  totals: {
    ...KPIS,
    revenue: "4.00",
    direct_cost: "1.00",
    contribution: "3.00",
    completed_orders: 1,
  },
};

const PRODUCTS: Products = {
  ...HEADER,
  rows: [
    {
      product_id: "p-cookie",
      product_name: "Cookie",
      product_type: "PRODUCED",
      is_active: true,
      underlying_units: "66.000000",
      revenue: "125.00",
      direct_cost: "34.60",
      contribution: "90.40",
      contribution_margin: "0.723200",
      completed_orders: 3,
      selling_options: [
        {
          kind: "STANDARD_OPTION",
          selling_option_id: "o-six",
          name: "6-pack",
          package_count: "4.000000",
          underlying_units: "24.000000",
          revenue: "48.00",
          direct_cost: "13.10",
          contribution: "34.90",
          contribution_margin: "0.727083",
          completed_orders: 2,
        },
        {
          kind: "CUSTOM_QUANTITY",
          selling_option_id: null,
          name: "Custom quantity",
          package_count: null,
          underlying_units: "30.000000",
          revenue: "55.00",
          direct_cost: "14.00",
          contribution: "41.00",
          contribution_margin: "0.745455",
          completed_orders: 1,
        },
      ],
    },
    {
      product_id: "p-lemonade",
      product_name: "Lemonade",
      product_type: "PURCHASED",
      is_active: false,
      underlying_units: "4.000000",
      revenue: "12.00",
      direct_cost: "1.20",
      contribution: "10.80",
      contribution_margin: "0.900000",
      completed_orders: 1,
      selling_options: [],
    },
  ],
  custom_items: {
    line_count: 1,
    completed_orders: 1,
    revenue: "5.00",
    direct_cost: "1.50",
    contribution: "3.50",
    contribution_margin: "0.700000",
  },
  order_level: {
    order_adjustments: "-5.00",
    manual_tax: "2.00",
    unattributed_direct_cost: "0.00",
    unreconciled_revenue: "0.00",
  },
  totals: KPIS,
  most_profitable: { product_id: "p-cookie", product_name: "Cookie", value: "90.40" },
  highest_margin: { product_id: "p-lemonade", product_name: "Lemonade", value: "0.900000" },
};

const CUSTOMERS: Customers = {
  ...HEADER,
  summary: {
    unique_customers: 3,
    new_customers: 2,
    returning_customers: 1,
    guest: {
      completed_orders: 2,
      revenue: "21.00",
      average_order_value: "10.50",
      share_of_completed_orders: "0.285714",
    },
  },
  items: [
    {
      customer_id: "c-bob",
      customer_name: "Bob",
      is_active: true,
      classification: "NEW",
      completed_orders: 2,
      revenue: "75.00",
      average_order_value: "37.50",
    },
    {
      customer_id: "c-cara",
      customer_name: "Cara",
      is_active: true,
      classification: "RETURNING",
      completed_orders: 1,
      revenue: "0.00",
      average_order_value: "0.00",
    },
  ],
  total: 2,
  limit: 25,
  offset: 0,
};

const ORDERS: Orders = {
  ...HEADER,
  summary: {
    completed_orders: 7,
    canceled_orders: 1,
    revenue: "166.00",
    average_order_value: "23.71",
    largest_completed_order: {
      order_id: "o2",
      order_number: "O2",
      final_total: "72.00",
      completed_at: "2026-10-13T01:30:00Z",
      customer_name: "Bob",
    },
    fulfillment_methods: [
      { method: "PICKUP", completed_orders: 3, revenue: "66.00" },
      { method: "DELIVERY", completed_orders: 2, revenue: "96.00" },
      { method: "OTHER", completed_orders: 1, revenue: "4.00" },
      { method: "UNSPECIFIED", completed_orders: 1, revenue: "0.00" },
    ],
    outstanding: {
      label: "Outstanding recorded balance — as of now",
      order_count: 4,
      balance: "56.00",
    },
  },
  items: [
    {
      order_id: "o2",
      order_number: "O2",
      status: "COMPLETED",
      reported_at: "2026-10-13T01:30:00Z",
      customer_id: "c-bob",
      customer_name: "Bob",
      fulfillment_method: "DELIVERY",
      final_total: "72.00",
      payments_total: "40.00",
      payment_status: "PARTIALLY_PAID",
      outstanding_balance: "32.00",
      direct_cost: "21.50",
      contribution: "50.50",
    },
    {
      order_id: "oc",
      order_number: "CANC-IN",
      status: "CANCELED",
      reported_at: "2026-10-11T12:00:00Z",
      customer_id: "c-bob",
      customer_name: "Bob",
      fulfillment_method: "PICKUP",
      final_total: "77.00",
      payments_total: "0.00",
      payment_status: null,
      outstanding_balance: null,
      direct_cost: null,
      contribution: null,
    },
  ],
  total: 2,
  limit: 25,
  offset: 0,
};

function analyticsResponder(overrides: Partial<Record<string, (url: URL) => Response>> = {}) {
  return (url: URL): Response => {
    const name = url.pathname.replace("/api/v1/analytics/", "");
    const custom = overrides[name];
    if (custom) return custom(url);
    switch (name) {
      case "overview":
        return jsonResponse(
          200,
          url.searchParams.get("compare") === "true" ? OVERVIEW_COMPARE : OVERVIEW_PLAIN,
        );
      case "trends":
        return jsonResponse(200, TRENDS);
      case "products":
        return jsonResponse(200, PRODUCTS);
      case "customers":
        return jsonResponse(200, CUSTOMERS);
      case "orders":
        return jsonResponse(200, ORDERS);
      default:
        return jsonResponse(404, { error: { code: "X", message: "x", issues: [] } });
    }
  };
}

function calls(fetchMock: ReturnType<typeof stubFetch>, name: string): URL[] {
  return fetchMock.mock.calls
    .map((c) => new URL(c[0] as string, "http://localhost"))
    .filter((u) => u.pathname === `/api/v1/analytics/${name}`);
}

// ------------------------------------------------------------------ navigation / shell

describe("Analytics shell", () => {
  it("is reachable from primary navigation and shows the page", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/dashboard");
    const link = await screen.findByRole("link", { name: "Analytics" });
    expect(link).toHaveAttribute("href", "/app/analytics");
  });

  it("renders the heading, four tabs and the default Last 30 days request", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics");
    expect(await screen.findByRole("heading", { name: "Analytics" })).toBeInTheDocument();
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(["Overview", "Products", "Customers", "Orders"]);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    await screen.findByTestId("kpi-revenue");
    expect(calls(fetchMock, "overview")[0].searchParams.get("range")).toBe("LAST_30_DAYS");
    expect(screen.getByRole("button", { name: "Last 30 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

// ------------------------------------------------------------------ Overview

describe("Overview", () => {
  it("shows KPI cards with recorded-revenue wording and internal metrics set apart", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&compare=0");
    const revenue = await screen.findByTestId("kpi-revenue");
    expect(within(revenue).getByText("Revenue (recorded Order revenue)")).toBeInTheDocument();
    expect(within(revenue).getByText("$166.00")).toBeInTheDocument();
    expect(within(screen.getByTestId("kpi-orders")).getByText("7")).toBeInTheDocument();
    expect(within(screen.getByTestId("kpi-aov")).getByText("$23.71")).toBeInTheDocument();

    const internal = screen.getByRole("region", { name: "Internal metrics" });
    expect(within(internal).getByText(/not a customer-facing amount/)).toBeInTheDocument();
    expect(within(internal).getByTestId("kpi-direct-cost")).toHaveTextContent("$47.05");
    expect(within(internal).getByTestId("kpi-contribution")).toHaveTextContent("$118.95");
    expect(within(internal).getByTestId("kpi-margin")).toHaveTextContent("71.7%");
    // Customer-facing Revenue is NOT inside the internal group.
    expect(within(internal).queryByTestId("kpi-revenue")).toBeNull();
    expect(screen.queryByText(/vs previous period/)).toBeNull();
  });

  it("labels the comparison as KPI-only and shows deltas when enabled (default)", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS");
    const toggle = await screen.findByLabelText("Compare overview KPIs to previous period");
    expect(toggle).toBeChecked();
    expect(calls(fetchMock, "overview")[0].searchParams.get("compare")).toBe("true");

    const revenue = await screen.findByTestId("kpi-revenue");
    await waitFor(() => expect(revenue).toHaveTextContent("+$127.00 (+325.6%)"));
    expect(screen.getByTestId("kpi-orders")).toHaveTextContent("+5 (+250.0%)");
    expect(screen.getByTestId("kpi-direct-cost")).toHaveTextContent("+$36.05 (+327.7%)");
    expect(screen.getByTestId("kpi-margin")).toHaveTextContent("-0.1 pts");
    expect(screen.getByTestId("kpi-aov")).toHaveTextContent("+$4.21 (+21.6%)");
    const note = screen.getByTestId("comparison-note");
    expect(note).toHaveTextContent("Oct 3, 2026 – Oct 9, 2026 (7 days)");
    expect(note).toHaveTextContent("KPI cards only");
  });

  it("turning comparison off refetches without compare and removes deltas", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS");
    const toggle = await screen.findByLabelText("Compare overview KPIs to previous period");
    await waitFor(() =>
      expect(screen.getByTestId("kpi-revenue")).toHaveTextContent("vs previous period"),
    );
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(
        calls(fetchMock, "overview").some((u) => u.searchParams.get("compare") === "false"),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText(/vs previous period/)).toBeNull());
  });

  it("shows no-prior-data text instead of fabricating growth", async () => {
    stubFetch(
      analyticsResponder({
        overview: () =>
          jsonResponse(200, {
            ...OVERVIEW_COMPARE,
            previous: {
              revenue: "0.00",
              direct_cost: "0.00",
              contribution: "0.00",
              contribution_margin: null,
              completed_orders: 0,
              average_order_value: null,
            },
            deltas: {
              ...OVERVIEW_COMPARE.deltas,
              revenue: { absolute: "166.00", percent_change: null },
            },
            previous_has_data: false,
          }),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS");
    const revenue = await screen.findByTestId("kpi-revenue");
    await waitFor(() => expect(revenue).toHaveTextContent("No prior-period completed-order data"));
    expect(revenue).not.toHaveTextContent("%");
  });

  it("zero-order period renders safe null states and an empty message", async () => {
    stubFetch(
      analyticsResponder({
        overview: () =>
          jsonResponse(200, {
            ...OVERVIEW_PLAIN,
            current: {
              revenue: "0.00",
              direct_cost: "0.00",
              contribution: "0.00",
              contribution_margin: null,
              completed_orders: 0,
              average_order_value: null,
            },
          }),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS&compare=0");
    expect(await screen.findByTestId("kpi-margin")).toHaveTextContent("—");
    expect(screen.getByTestId("kpi-aov")).toHaveTextContent("—");
    expect(screen.getByText("No Orders were completed in this period.")).toBeInTheDocument();
  });

  it("renders the trend chart with a readable data table and CSV link", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&compare=0");
    expect(await screen.findByTestId("trend-chart")).toHaveAccessibleName(
      "Revenue, Direct Cost and Contribution, daily",
    );
    const table = await screen.findByRole("table", { name: "Trend data (daily)" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(1 + 2 + 1); // header + 2 buckets + total
    expect(within(rows[1]).getByText("2026-10-10")).toBeInTheDocument();
    expect(within(rows[2]).getAllByText("$0.00").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Download trend CSV" })).toHaveAttribute(
      "href",
      "/api/v1/analytics/trends.csv?range=LAST_7_DAYS",
    );
  });

  it("shows a retryable error banner when the overview fails", async () => {
    let failing = true;
    stubFetch(
      analyticsResponder({
        overview: () =>
          failing
            ? jsonResponse(500, { error: { code: "X", message: "Boom", issues: [] } })
            : jsonResponse(200, OVERVIEW_PLAIN),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS&compare=0");
    expect(await screen.findByRole("alert")).toHaveTextContent("Boom");
    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("kpi-revenue")).toHaveTextContent("$166.00");
  });
});

// ------------------------------------------------------------------ Date range control

describe("Date range control", () => {
  it("preset buttons drive the request and the URL", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    const router = renderAt("/app/analytics?compare=0");
    await screen.findByTestId("kpi-revenue");
    fireEvent.click(screen.getByRole("button", { name: "This year" }));
    await waitFor(() =>
      expect(
        calls(fetchMock, "overview").some((u) => u.searchParams.get("range") === "THIS_YEAR"),
      ).toBe(true),
    );
    expect(router.state.location.search).toContain("range=THIS_YEAR");
  });

  it("applies an inclusive custom range with both dates", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?compare=0");
    await screen.findByTestId("kpi-revenue");
    fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-10-01" } });
    fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2026-10-16" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply custom range" }));
    await waitFor(() => {
      const custom = calls(fetchMock, "overview").find(
        (u) => u.searchParams.get("range") === "CUSTOM",
      );
      expect(custom?.searchParams.get("start_date")).toBe("2026-10-01");
      expect(custom?.searchParams.get("end_date")).toBe("2026-10-16");
    });
  });

  it("blocks a custom range with missing or reversed dates before any request", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?compare=0");
    await screen.findByTestId("kpi-revenue");
    const before = calls(fetchMock, "overview").length;
    fireEvent.click(screen.getByRole("button", { name: "Apply custom range" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Choose both a start date and an end date.",
    );
    fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-10-16" } });
    fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2026-10-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply custom range" }));
    expect(screen.getByRole("alert")).toHaveTextContent("cannot be after");
    expect(calls(fetchMock, "overview").length).toBe(before);
  });

  it("surfaces the server's structured 422 for an invalid range", async () => {
    stubFetch(
      analyticsResponder({
        overview: () =>
          jsonResponse(422, {
            error: {
              code: "ANALYTICS_RANGE_INVALID",
              message: "The requested Analytics range is not valid.",
              issues: [],
            },
          }),
      }),
    );
    renderAt("/app/analytics?range=CUSTOM&start=2026-10-16&end=2026-10-01&compare=0");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The requested Analytics range is not valid.",
    );
  });
});

describe("Range-too-large rejection", () => {
  const tooLarge = () =>
    jsonResponse(422, {
      error: {
        code: "ANALYTICS_RANGE_INVALID",
        message: "The requested Analytics range is not valid.",
        issues: [
          {
            severity: "ERROR",
            code: "ANALYTICS_RANGE_TOO_LARGE",
            message: "A Custom Range can span at most 3,660 days (requested 5,845).",
            field: "end_date",
            resource: null,
            details: { requested_days: 5845, maximum_days: 3660 },
          },
        ],
      },
    });

  it("shows the server's message, offers no Retry, and renders no partial data", async () => {
    stubFetch(analyticsResponder({ overview: tooLarge, trends: tooLarge }));
    renderAt("/app/analytics?range=CUSTOM&start=2010-01-01&end=2026-01-01&compare=0");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "A Custom Range can span at most 3,660 days (requested 5,845).",
    );
    expect(alert).toHaveTextContent("Choose a different period above");
    expect(within(alert).queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByTestId("kpi-revenue")).toBeNull();
    expect(screen.queryByTestId("trend-chart")).toBeNull();
    // One banner, not one per failing query.
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("every tab shows the same structured rejection", async () => {
    stubFetch(analyticsResponder({ products: tooLarge, customers: tooLarge, orders: tooLarge }));
    for (const tab of ["products", "customers", "orders"]) {
      renderAt(`/app/analytics?range=CUSTOM&start=2010-01-01&end=2026-01-01&tab=${tab}`);
      expect(await screen.findByRole("alert")).toHaveTextContent("at most 3,660 days");
      cleanup();
    }
  });
});

// ------------------------------------------------------------------ Products

describe("Products tab", () => {
  it("separates units from packages and labels the two different 'best' metrics", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=products");
    const table = await screen.findByRole("table", { name: /Product results/ });
    expect(within(table).getByRole("columnheader", { name: "Units sold" })).toBeInTheDocument();
    const cookie = within(table)
      .getByRole("rowheader", { name: /Cookie/ })
      .closest("tr")!;
    expect(cookie).toHaveTextContent("66");
    expect(cookie).toHaveTextContent("$125.00");
    expect(cookie).toHaveTextContent("$90.40");
    expect(cookie).toHaveTextContent("72.3%");

    expect(screen.getByTestId("highlight-profitable")).toHaveTextContent(
      "Most profitable — highest total Contribution",
    );
    expect(screen.getByTestId("highlight-profitable")).toHaveTextContent("Cookie $90.40");
    expect(screen.getByTestId("highlight-margin")).toHaveTextContent(
      "Highest margin — Contribution ÷ Revenue",
    );
    expect(screen.getByTestId("highlight-margin")).toHaveTextContent("Lemonade 90.0%");

    fireEvent.click(screen.getByRole("button", { name: "Show selling options for Cookie" }));
    expect(screen.getByText("4 packages")).toBeInTheDocument();
    expect(screen.getByText("units only — no package count")).toBeInTheDocument();
    expect(screen.getAllByTestId("option-p-cookie")).toHaveLength(2);
  });

  it("shows reconciliation rows so the table ties to the Overview", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=products");
    await screen.findByRole("table", { name: /Product results/ });
    expect(screen.getByTestId("row-custom-items")).toHaveTextContent("Custom items (no Product)");
    expect(screen.getByTestId("row-custom-items")).toHaveTextContent("$5.00");
    expect(screen.getByTestId("row-adjustments")).toHaveTextContent("-$5.00");
    expect(screen.getByTestId("row-tax")).toHaveTextContent("$2.00");
    expect(screen.getByTestId("row-unattributed")).toHaveTextContent("$0.00");
    expect(screen.getByTestId("row-totals")).toHaveTextContent("$166.00");
    expect(screen.getByTestId("row-totals")).toHaveTextContent("$47.05");
    expect(screen.getByText("Archived")).toBeInTheDocument(); // Lemonade is archived: label only
  });

  it("sorting refetches with the chosen sort and the CSV link mirrors it", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=products");
    await screen.findByRole("table", { name: /Product results/ });
    expect(screen.getByRole("link", { name: "Download products CSV" })).toHaveAttribute(
      "href",
      "/api/v1/analytics/products.csv?range=LAST_7_DAYS&sort=contribution",
    );
    fireEvent.change(screen.getByLabelText("Sort products by"), { target: { value: "margin" } });
    await waitFor(() =>
      expect(
        calls(fetchMock, "products").some((u) => u.searchParams.get("sort") === "margin"),
      ).toBe(true),
    );
    expect(screen.getByRole("link", { name: "Download products CSV" })).toHaveAttribute(
      "href",
      "/api/v1/analytics/products.csv?range=LAST_7_DAYS&sort=margin",
    );
  });

  it("shows an empty state when nothing sold", async () => {
    stubFetch(
      analyticsResponder({
        products: () =>
          jsonResponse(200, {
            ...PRODUCTS,
            rows: [],
            custom_items: null,
            most_profitable: null,
            highest_margin: null,
          }),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=products");
    expect(
      await screen.findByText("No Product sales were completed in this period."),
    ).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ Customers

describe("Customers tab", () => {
  it("shows new/returning counts, Guest activity apart from customers, and rows", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=customers");
    expect(await screen.findByTestId("kpi-unique-customers")).toHaveTextContent("3");
    expect(screen.getByTestId("kpi-new-customers")).toHaveTextContent("2");
    expect(screen.getByTestId("kpi-returning-customers")).toHaveTextContent("1");
    const guest = screen.getByTestId("kpi-guest");
    expect(guest).toHaveTextContent("2");
    expect(guest).toHaveTextContent("$21.00 Revenue");
    expect(guest).toHaveTextContent("AOV $10.50");
    expect(guest).toHaveTextContent("28.6% of completed Orders");

    const table = screen.getByRole("table", { name: /Customers with a Completed Order/ });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("Bob");
    expect(rows[1]).toHaveTextContent("New");
    expect(rows[1]).toHaveTextContent("$75.00");
    expect(rows[1]).toHaveTextContent("$37.50");
    expect(rows[2]).toHaveTextContent("Returning");
    expect(within(table).queryByText("Guest")).toBeNull();
    expect(screen.getByTestId("pager-range")).toHaveTextContent("1–2 of 2 customers");
  });

  it("changing the sort resets paging and updates the CSV link", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=customers");
    await screen.findByTestId("kpi-unique-customers");
    fireEvent.change(screen.getByLabelText("Sort customers by"), {
      target: { value: "completed_orders" },
    });
    await waitFor(() =>
      expect(
        calls(fetchMock, "customers").some(
          (u) =>
            u.searchParams.get("sort") === "completed_orders" &&
            u.searchParams.get("offset") === "0",
        ),
      ).toBe(true),
    );
    expect(screen.getByRole("link", { name: "Download customers CSV" })).toHaveAttribute(
      "href",
      "/api/v1/analytics/customers.csv?range=LAST_7_DAYS&sort=completed_orders",
    );
  });

  it("pages through customers with server offsets", async () => {
    const fetchMock = stubFetch(
      analyticsResponder({
        customers: (url) =>
          jsonResponse(200, {
            ...CUSTOMERS,
            total: 60,
            limit: 25,
            offset: Number(url.searchParams.get("offset")),
          }),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=customers");
    await screen.findByTestId("kpi-unique-customers");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(calls(fetchMock, "customers").some((u) => u.searchParams.get("offset") === "25")).toBe(
        true,
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("pager-range")).toHaveTextContent("26–50 of 60 customers"),
    );
  });
});

// ------------------------------------------------------------------ Orders

describe("Orders tab", () => {
  it("labels the as-of-now outstanding balance and keeps Canceled Orders out of financials", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=orders");
    const outstanding = await screen.findByTestId("kpi-outstanding");
    expect(outstanding).toHaveTextContent("Outstanding recorded balance — as of now");
    expect(outstanding).toHaveTextContent("$56.00");
    expect(outstanding).toHaveTextContent("4 completed Orders with a balance");
    expect(screen.getByTestId("kpi-canceled")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-completed")).toHaveTextContent("7");
    expect(screen.getByTestId("kpi-largest")).toHaveTextContent("$72.00");
    expect(screen.getByTestId("kpi-largest")).toHaveTextContent("O2 · Bob");

    const methods = screen.getByRole("table", { name: /fulfillment method/ });
    expect(within(methods).getByRole("rowheader", { name: "Not specified" })).toBeInTheDocument();
    expect(within(methods).getAllByRole("row")).toHaveLength(5);

    const table = screen.getByRole("table", { name: /completed or canceled/ });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Partially paid");
    expect(rows[1]).toHaveTextContent("$32.00");
    expect(rows[1]).toHaveTextContent("$50.50");
    // The Canceled row never presents a balance, cost or contribution.
    expect(rows[2]).toHaveTextContent("Canceled");
    expect(rows[2]).toHaveTextContent("$77.00");
    expect(rows[2].textContent?.match(/—/g)?.length).toBe(4);
  });

  it("labels an unresolved customer safely and a true Guest as Guest", async () => {
    const row = ORDERS.items[0];
    stubFetch(
      analyticsResponder({
        orders: () =>
          jsonResponse(200, {
            ...ORDERS,
            items: [
              {
                ...row,
                order_id: "ou",
                order_number: "UNRES",
                customer_id: null,
                customer_name: "(unavailable customer)",
              },
              {
                ...row,
                order_id: "og",
                order_number: "GUESTY",
                customer_id: null,
                customer_name: null,
              },
            ],
            total: 2,
          }),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=orders");
    const table = await screen.findByRole("table", { name: /completed or canceled/ });
    const unresolved = within(table).getByRole("row", { name: /UNRES/ });
    expect(unresolved).toHaveTextContent("(unavailable customer)");
    expect(unresolved).not.toHaveTextContent("Guest");
    expect(within(table).getByRole("row", { name: /GUESTY/ })).toHaveTextContent("Guest");
  });

  it("filters send server parameters and the CSV link mirrors them", async () => {
    const fetchMock = stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=orders");
    await screen.findByTestId("kpi-outstanding");
    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "COMPLETED" } });
    fireEvent.click(screen.getByLabelText("Outstanding balance only"));
    await waitFor(() =>
      expect(
        calls(fetchMock, "orders").some(
          (u) =>
            u.searchParams.get("status") === "COMPLETED" &&
            u.searchParams.get("outstanding_only") === "true",
        ),
      ).toBe(true),
    );
    expect(screen.getByRole("link", { name: "Download orders CSV" })).toHaveAttribute(
      "href",
      "/api/v1/analytics/orders.csv?range=LAST_7_DAYS&status=COMPLETED&outstanding_only=true&sort=reported_at",
    );
  });

  it("shows an empty state when no orders match", async () => {
    stubFetch(
      analyticsResponder({
        orders: () =>
          jsonResponse(200, {
            ...ORDERS,
            items: [],
            total: 0,
            summary: {
              ...ORDERS.summary,
              completed_orders: 0,
              canceled_orders: 0,
              revenue: "0.00",
              average_order_value: null,
              largest_completed_order: null,
              outstanding: { ...ORDERS.summary.outstanding, order_count: 0, balance: "0.00" },
            },
          }),
      }),
    );
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=orders");
    expect(await screen.findByText("No matching Orders in this period.")).toBeInTheDocument();
    expect(screen.getByTestId("kpi-largest")).toHaveTextContent("No Completed Orders");
  });
});

// ------------------------------------------------------------------ Tabs & accessibility

describe("Tabs and accessibility", () => {
  it("tabs are keyboard operable and the selected panel is associated", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&compare=0");
    await screen.findByTestId("kpi-revenue");
    const overviewTab = screen.getByRole("tab", { name: "Overview" });
    overviewTab.focus();
    fireEvent.keyDown(overviewTab, { key: "ArrowRight" });
    expect(
      await screen.findByRole("tab", { name: "Products", selected: true }),
    ).toBeInTheDocument();
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", "analytics-tab-products");
  });

  it("every table has a caption and header cells with scope", async () => {
    stubFetch(analyticsResponder());
    renderAt("/app/analytics?range=LAST_7_DAYS&tab=orders");
    await screen.findByTestId("kpi-outstanding");
    for (const table of screen.getAllByRole("table")) {
      expect(table.querySelector("caption")).not.toBeNull();
      expect(table.querySelectorAll("th[scope='col']").length).toBeGreaterThan(0);
    }
  });
});
