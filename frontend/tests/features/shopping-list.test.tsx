// Phase 9 Shopping List behavior tests (Spec §12.12; Acceptance Matrix F2/F3/F4) plus the
// approved cross-screen consistency displays (Ingredient Reserved/Available, Surplus
// Allocated/Available, Production Plan shortage + affected orders). The frontend never
// computes a shortage: every figure below is rendered from the server payload.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";
import type { ShoppingList } from "../../src/features/production/shoppingListApi";

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

type Responder = (url: string) => Response;

function stubFetch(respond: Responder) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.endsWith("/api/v1/auth/me")) {
      return Promise.resolve(ME());
    }
    return Promise.resolve(respond(url));
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
  return client;
}

const timeline = (shortageEnd: string) => [
  {
    demand_date: "2026-06-10",
    cumulative_reserved_through_date: "60.000000",
    cumulative_shortage_through_date: "0.000000",
  },
  {
    demand_date: "2026-06-11",
    cumulative_reserved_through_date: "60.000000",
    cumulative_shortage_through_date: "0.000000",
  },
  {
    demand_date: "2026-06-12",
    cumulative_reserved_through_date: "120.000000",
    cumulative_shortage_through_date: shortageEnd,
  },
];

const FLOUR = {
  ingredient_id: "i1",
  ingredient_name: "Flour",
  canonical_unit: "g",
  physical_quantity: "100.000000",
  reserved_through_end: "120.000000",
  shortage_quantity: "20.000000",
  needed_by_date: "2026-06-12",
  by_date: timeline("20.000000"),
  contributors: [
    {
      requirement_id: "r1",
      product_id: "p1",
      product_name: "Cookie",
      recipe_revision_id: "rev1",
      demand_date: "2026-06-10",
      reserved_quantity: "60.000000",
      is_overdue: true,
      is_in_production: true,
      orders: [
        {
          order_id: "o1",
          order_number: "ORD-A",
          status: "CONFIRMED",
          customer_name: "Alice",
          fulfillment_date: "2026-06-10",
          fulfillment_time: "09:00:00",
          demand_quantity: "12.000000",
        },
      ],
    },
    {
      requirement_id: "r2",
      product_id: "p1",
      product_name: "Cookie",
      recipe_revision_id: "rev1",
      demand_date: "2026-06-12",
      reserved_quantity: "60.000000",
      is_overdue: false,
      is_in_production: false,
      orders: [],
    },
  ],
};

const SUGAR = {
  ...FLOUR,
  ingredient_id: "i2",
  ingredient_name: "Sugar",
  shortage_quantity: "190.000000",
  reserved_through_end: "200.000000",
  physical_quantity: "10.000000",
  contributors: [],
};

const SALT = {
  ...FLOUR,
  ingredient_id: "i3",
  ingredient_name: "Salt",
  shortage_quantity: "0.000000",
  physical_quantity: "500.000000",
  reserved_through_end: "5.000000",
  needed_by_date: null,
  contributors: [],
};

function report(overrides: Partial<ShoppingList> = {}): ShoppingList {
  return {
    business_today: "2026-06-10",
    range: { start_date: "2026-06-10", end_date: "2026-06-17", horizon_days: 7, is_custom: false },
    shortages: [SUGAR, FLOUR],
    covered: [SALT],
    incomplete_recipe: [],
    ...overrides,
  };
}

describe("Shopping List page", () => {
  it("renders shortages first (server order), covered items collapsed, and the range summary", async () => {
    stubFetch(() => jsonResponse(200, report()));
    renderAt("/app/production/shopping-list");

    await screen.findByRole("heading", { name: "Need to buy (2)" });
    const rows = screen.getAllByRole("row").slice(1, 3);
    expect(within(rows[0]).getByText("Sugar")).toBeInTheDocument();
    expect(within(rows[0]).getByText(/Short 190 g/)).toBeInTheDocument();
    expect(within(rows[1]).getByText("Flour")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/Short 20 g/)).toBeInTheDocument();

    expect(screen.getByTestId("range-summary")).toHaveTextContent(/7-day horizon/);
    expect(screen.getByText(/Covered by current stock \(1\)/)).toBeInTheDocument();
  });

  it("highlights the active preset from the server's resolved range and switches presets", async () => {
    const fetchMock = stubFetch((url) =>
      jsonResponse(
        200,
        url.includes("horizon_days=3")
          ? report({
              range: {
                start_date: "2026-06-10",
                end_date: "2026-06-13",
                horizon_days: 3,
                is_custom: false,
              },
            })
          : report(),
      ),
    );
    renderAt("/app/production/shopping-list");
    await screen.findByRole("heading", { name: "Need to buy (2)" });
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "3 days" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "3 days" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/shopping-list?horizon_days=3")),
    ).toBe(true);
  });

  it("validates a custom range before sending and sends valid ranges", async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, report()));
    renderAt("/app/production/shopping-list");
    await screen.findByRole("heading", { name: "Need to buy (2)" });

    fireEvent.click(screen.getByRole("button", { name: "Apply custom range" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/both a start date and an end date/i);

    fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-06-15" } });
    fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2026-06-12" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply custom range" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/cannot be before the start/i);

    fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2026-06-20" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply custom range" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("start_date=2026-06-15&end_date=2026-06-20"),
        ),
      ).toBe(true),
    );
  });

  it("shows the server's structured validation message for a rejected range", async () => {
    stubFetch(() =>
      jsonResponse(422, {
        error: {
          code: "SHOPPING_LIST_RANGE_INVALID",
          message: "The requested Shopping List range is not valid.",
          issues: [
            {
              severity: "ERROR",
              code: "SHOPPING_LIST_RANGE_TOO_LONG",
              message: "A custom range can span at most 90 days.",
              field: "end_date",
              resource: "shopping_list",
              details: {},
            },
          ],
        },
      }),
    );
    renderAt("/app/production/shopping-list");
    expect(await screen.findByText("A custom range can span at most 90 days.")).toBeInTheDocument();
  });

  it("expands the contributing-demand drilldown with overdue/in-production labels and orders", async () => {
    stubFetch(() => jsonResponse(200, report()));
    renderAt("/app/production/shopping-list");
    await screen.findByRole("heading", { name: "Need to buy (2)" });

    const toggle = screen.getByRole("button", { name: /show contributing demand for flour/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    const drilldown = await screen.findByTestId("drilldown-i1");
    expect(within(drilldown).getByText("Overdue")).toBeInTheDocument();
    expect(within(drilldown).getByText("In production")).toBeInTheDocument();
    expect(within(drilldown).getByRole("link", { name: "ORD-A" })).toHaveAttribute(
      "href",
      "/app/orders/o1",
    );
    expect(within(drilldown).getByText(/Alice/)).toBeInTheDocument();
    expect(within(drilldown).getByText(/short 20 g/)).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.queryByTestId("drilldown-i1")).not.toBeInTheDocument();
  });

  it("shows the empty and nothing-to-buy states", async () => {
    stubFetch(() => jsonResponse(200, report({ shortages: [], covered: [] })));
    renderAt("/app/production/shopping-list");
    expect(await screen.findByTestId("no-shortages")).toHaveTextContent(
      /no ingredient demand in this range/i,
    );
    cleanup();

    stubFetch(() => jsonResponse(200, report({ shortages: [], covered: [SALT] })));
    renderAt("/app/production/shopping-list");
    expect(await screen.findByTestId("no-shortages")).toHaveTextContent(/nothing to buy/i);
  });

  it("surfaces incomplete-recipe demand as a notice without inventing ingredients", async () => {
    stubFetch(() =>
      jsonResponse(
        200,
        report({
          shortages: [],
          covered: [],
          incomplete_recipe: [
            {
              requirement_id: "r9",
              product_id: "p9",
              product_name: "Mystery Loaf",
              demand_date: "2026-06-10",
              demand_quantity: "6.000000",
            },
          ],
        }),
      ),
    );
    renderAt("/app/production/shopping-list");
    const notice = await screen.findByTestId("incomplete-recipe-notice");
    expect(notice).toHaveTextContent(/no applicable recipe/i);
    expect(within(notice).getByRole("link", { name: "Mystery Loaf" })).toHaveAttribute(
      "href",
      "/app/products/p9",
    );
  });

  it("shows loading and error-with-retry states", async () => {
    stubFetch(() => jsonResponse(500, { error: { code: "X", message: "kaput", issues: [] } }));
    renderAt("/app/production/shopping-list");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("kaput");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("refetches on mount so it shows changes made on other pages", async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, report()));
    const client = renderAt("/app/production/shopping-list");
    await screen.findByRole("heading", { name: "Need to buy (2)" });
    const calls = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/v1/shopping-list")).length;
    expect(calls()).toBe(1);

    cleanup();
    const router = createMemoryRouter(routes, {
      initialEntries: ["/app/production/shopping-list"],
    });
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <RouterProvider router={router} />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await vi.waitFor(() => expect(calls()).toBe(2));
  });

  it("is reachable from the Production Plan and the main navigation", async () => {
    stubFetch(() => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }));
    renderAt("/app/production");
    await screen.findByRole("heading", { name: "Production Plan" });
    const links = screen.getAllByRole("link", { name: "Shopping List" });
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const link of links) {
      expect(link).toHaveAttribute("href", "/app/production/shopping-list");
    }
  });
});

describe("Cross-screen consistency displays (approved Q3)", () => {
  const REQUIREMENT = {
    id: "req1",
    product_id: "p1",
    product_name: "Cookie",
    recipe_revision_id: "rev1",
    demand_date: "2026-06-12",
    calculation_status: "CALCULATED",
    confirmed_demand_quantity: "12.000000",
    surplus_allocated_quantity: "0.000000",
    production_demand_quantity: "12.000000",
    recommended_batches: 1,
    expected_output_quantity: "12.000000",
    expected_excess_quantity: "0.000000",
    estimated_active_minutes: 30,
    estimated_elapsed_minutes: null,
    suggested_start_at: null,
    estimated_ingredient_cost: null,
    estimated_labor_cost: null,
    estimated_direct_production_cost: null,
    has_shortage: true,
    ingredient_needs: [
      {
        ingredient_id: "i1",
        ingredient_name: "Flour",
        canonical_unit: "g",
        required_quantity: "60.000000",
        physical_quantity: "100.000000",
        shortage_quantity: "20.000000",
      },
      {
        ingredient_id: "i4",
        ingredient_name: "Butter",
        canonical_unit: "g",
        required_quantity: "10.000000",
        physical_quantity: "100.000000",
        shortage_quantity: "0.000000",
      },
    ],
    affected_orders: [
      {
        order_id: "o1",
        order_number: "ORD-A",
        status: "CONFIRMED",
        customer_name: null,
        fulfillment_date: "2026-06-12",
        fulfillment_time: "10:00:00",
        demand_quantity: "12.000000",
      },
    ],
  };

  it("Production Plan shows the shortage, the affected orders, and a Shopping List link", async () => {
    stubFetch(() => jsonResponse(200, { items: [REQUIREMENT], total: 1, limit: 200, offset: 0 }));
    renderAt("/app/production");
    await screen.findByText("Cookie");

    expect(screen.getByText("Ingredient shortage")).toBeInTheDocument();
    expect(screen.getByText(/Flour: short 20 g \(needs 60\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Butter: short/)).not.toBeInTheDocument();
    const order = screen.getByRole("link", { name: "ORD-A" });
    expect(order).toHaveAttribute("href", "/app/orders/o1");
    expect(order.closest("li")).toHaveTextContent(/Guest/);
    expect(screen.getByRole("link", { name: "See Shopping List" })).toHaveAttribute(
      "href",
      "/app/production/shopping-list",
    );
  });

  it("Production Plan says 'No shortage' when nothing is short", async () => {
    stubFetch(() =>
      jsonResponse(200, {
        items: [{ ...REQUIREMENT, has_shortage: false, ingredient_needs: [] }],
        total: 1,
        limit: 200,
        offset: 0,
      }),
    );
    renderAt("/app/production");
    expect(await screen.findByText("No shortage")).toBeInTheDocument();
  });

  it("Ingredient detail shows Physical, Reserved and Available and explains they are derived", async () => {
    stubFetch(() =>
      jsonResponse(200, {
        id: "i1",
        name: "Flour",
        measurement_family: "WEIGHT",
        canonical_unit: "g",
        is_active: true,
        version: 1,
        physical_quantity: "100.000000",
        reserved_quantity: "120.000000",
        available_quantity: "-20.000000",
        weighted_average_unit_cost: "0.020000",
        latest_purchase_unit_cost: null,
        replacement_unit_cost: null,
        effective_replacement_cost: null,
      }),
    );
    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });

    expect(screen.getByText("Physical quantity")).toBeInTheDocument();
    expect(screen.getByText("Reserved for confirmed demand")).toBeInTheDocument();
    expect(screen.getByText("Available")).toBeInTheDocument();
    expect(screen.getByText(/120 g/)).toBeInTheDocument();
    expect(screen.getByText(/-20 g/)).toHaveTextContent(/reservations exceed stock/);
    expect(screen.getByText(/they are not editable/i)).toBeInTheDocument();
  });

  it("Surplus list shows Physical, Allocated and Available as distinct columns", async () => {
    stubFetch(() =>
      jsonResponse(200, {
        items: [
          {
            id: "s1",
            product_id: "p1",
            product_name: "Cookie",
            source_production_run_id: "run1",
            physical_quantity: "6.000000",
            allocated_quantity: "4.000000",
            available_quantity: "2.000000",
            unit_cost_basis: "0.500000",
            source_total_cost_basis: "3.00",
            produced_at: "2026-06-01T12:00:00Z",
            usable_through_date: null,
            is_reusable: true,
            notes: null,
            version: 1,
          },
        ],
        total: 1,
        limit: 200,
        offset: 0,
      }),
    );
    renderAt("/app/surplus");
    await screen.findByRole("heading", { name: "Surplus" });
    const table = await screen.findByRole("table");
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(
      expect.arrayContaining(["Physical quantity", "Allocated", "Available"]),
    );
    const cells = within(table)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells).toEqual(expect.arrayContaining(["6", "4", "2"]));
  });
});
