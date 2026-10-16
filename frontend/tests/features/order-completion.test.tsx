// Order Completion / Fulfillment UI tests: the Complete action (READY only), confirmation
// dialog, double-submit protection, structured error handling (coverage lost, stale
// version, production locked), the Completed state, the historical direct-cost panel, and
// refresh of dependent views. The frontend computes nothing: every figure is rendered from
// the server response.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";

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

const LINE = {
  id: "line-1",
  line_type: "CUSTOM_QUANTITY",
  product_id: "p1",
  selling_option_id: null,
  display_name_snapshot: "Lemonade",
  package_quantity: "1.000000",
  underlying_quantity: "4.000000",
  charged_unit_price_snapshot: "20.00",
  line_subtotal: "20.00",
  packaging_cost_per_package_snapshot: "0.00",
  packaging_cost_total_snapshot: "0.00",
  price_override_reason: null,
  custom_direct_cost_estimate: null,
  custom_active_time_minutes: null,
  manual_fulfillment_required: false,
  manual_fulfillment_satisfied: false,
  notes: null,
};

const READY_ORDER = {
  id: "o1",
  order_number: "ORD-READY0000001",
  status: "READY",
  customer_id: null,
  fulfillment_date: "2026-06-10",
  fulfillment_time: null,
  fulfillment_method: null,
  fulfillment_details: null,
  fulfillment_notes: null,
  internal_notes: null,
  subtotal: "20.00",
  order_adjustment: "0.00",
  adjustment_description: null,
  manual_tax: "0.00",
  final_total: "20.00",
  estimated_direct_cost: null,
  estimated_contribution: null,
  estimated_contribution_margin: null,
  version: 3,
  lines: [LINE],
  payments: [],
  payments_total: "0.00",
  payment_status: "UNPAID",
  overpayment_amount: null,
  is_confirmable: false,
  confirmation_issues: [],
  production_locked: false,
  ready_at: "2026-06-09T15:00:00Z",
  was_ever_ready: false,
  completed_at: null,
  historical_cost: null,
};

const COMPLETED_ORDER = {
  ...READY_ORDER,
  status: "COMPLETED",
  version: 4,
  completed_at: "2026-06-10T18:30:00Z",
  historical_cost: {
    total: "7.35",
    by_type: {
      PRODUCTION: "0.00",
      SURPLUS: "0.00",
      PURCHASED_GOOD: "6.00",
      PACKAGING: "0.75",
      CUSTOM_DIRECT: "0.60",
    },
    allocations: [],
  },
};

type Handler = {
  method: string;
  pattern: RegExp;
  respond: (url: string, init?: RequestInit) => Response;
};

function mockFetch(handlers: Handler[]) {
  const mock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.endsWith("/api/v1/auth/me")) return Promise.resolve(ME());
    const handler = handlers.find((h) => h.method === method && h.pattern.test(url));
    if (!handler) throw new Error(`Unhandled fetch: ${method} ${url}`);
    return Promise.resolve(handler.respond(url, init));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function renderOrder() {
  const router = createMemoryRouter(routes, { initialEntries: ["/app/orders/o1"] });
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

const completeCalls = (mock: ReturnType<typeof mockFetch>) =>
  mock.mock.calls.filter(([url]) => String(url).endsWith("/orders/o1/complete"));

describe("Order Details — Complete Order", () => {
  it("offers Complete only for a READY order", async () => {
    for (const status of ["DRAFT", "CONFIRMED", "COMPLETED", "CANCELED"]) {
      mockFetch([
        {
          method: "GET",
          pattern: /\/api\/v1\/orders\/o1$/,
          respond: () => jsonResponse(200, { ...READY_ORDER, status }),
        },
      ]);
      renderOrder();
      await screen.findByText("ORD-READY0000001");
      expect(screen.queryByRole("button", { name: "Complete Order" })).not.toBeInTheDocument();
      cleanup();
    }
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, READY_ORDER),
      },
    ]);
    renderOrder();
    expect(await screen.findByRole("button", { name: "Complete Order" })).toBeEnabled();
  });

  it("asks for confirmation, explains consequences, and states payment is not required", async () => {
    const mock = mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, READY_ORDER),
      },
    ]);
    renderOrder();
    fireEvent.click(await screen.findByRole("button", { name: "Complete Order" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/cannot be undone/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/Payment is not required/)).toBeInTheDocument();
    expect(within(dialog).getByText(/\$0\.00 of \$20\.00 is recorded/)).toBeInTheDocument();
    expect(completeCalls(mock)).toHaveLength(0); // nothing sent until confirmed

    fireEvent.click(within(dialog).getByRole("button", { name: "Not yet" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(completeCalls(mock)).toHaveLength(0);
  });

  it("sends the expected version once, disables duplicate submission, then shows the Completed state", async () => {
    let completed = false;
    let resolveComplete: (r: Response) => void = () => {};
    const mock = mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, completed ? COMPLETED_ORDER : READY_ORDER),
      },
    ]);
    // The complete call hangs until we release it so the pending state is observable.
    const base = mock.getMockImplementation()!;
    mock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/orders/o1/complete")) {
        return new Promise<Response>((resolve) => {
          resolveComplete = (r) => {
            completed = true;
            resolve(r);
          };
        });
      }
      return base(url, init);
    });

    renderOrder();
    fireEvent.click(await screen.findByRole("button", { name: "Complete Order" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Complete order" });
    fireEvent.click(confirm);
    fireEvent.click(confirm); // double-click while pending

    await waitFor(() => expect(completeCalls(mock)).toHaveLength(1));
    expect(JSON.parse(String(completeCalls(mock)[0][1]?.body))).toEqual({ version: 3 });
    expect(within(dialog).getByRole("button", { name: "Completing…" })).toBeDisabled();

    resolveComplete(jsonResponse(200, COMPLETED_ORDER));
    expect(await screen.findByTestId("completed-panel")).toBeInTheDocument();
    expect(completeCalls(mock)).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Complete Order" })).not.toBeInTheDocument();
  });

  it("shows the historical direct cost with every category, incl. explicit zero, and no analytics", async () => {
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, COMPLETED_ORDER),
      },
    ]);
    renderOrder();
    const panel = await screen.findByTestId("historical-cost");
    expect(within(panel).getByText("Purchased goods").nextSibling).toHaveTextContent("$6.00");
    expect(within(panel).getByText("Packaging").nextSibling).toHaveTextContent("$0.75");
    expect(within(panel).getByText("Surplus used").nextSibling).toHaveTextContent("$0.00");
    expect(within(panel).getByText(/Total historical direct cost/).nextSibling).toHaveTextContent(
      "$7.35",
    );
    expect(within(panel).getByText(/not shown to the customer/i)).toBeInTheDocument();
    expect(within(panel).getByText(/not accounting profit/i)).toBeInTheDocument();
    expect(screen.getByTestId("completed-panel")).toHaveTextContent(/Wed, Jun 10|Jun 10/);
    // Phase 10 analytics are out of scope.
    expect(screen.queryByText(/contribution|average order value|\bAOV\b/i)).not.toBeInTheDocument();
  });

  it("surfaces a coverage-lost rejection as a clear, non-destructive error", async () => {
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, READY_ORDER),
      },
      {
        method: "POST",
        pattern: /\/orders\/o1\/complete$/,
        respond: () =>
          jsonResponse(409, {
            error: {
              code: "ORDER_COVERAGE_LOST",
              message: "This order is no longer fully covered.",
              issues: [
                {
                  severity: "ERROR",
                  code: "PURCHASED_STOCK_INSUFFICIENT",
                  message: "Purchased stock on hand no longer covers this order.",
                  field: null,
                  resource: "order",
                  details: {},
                },
              ],
            },
          }),
      },
    ]);
    renderOrder();
    fireEvent.click(await screen.findByRole("button", { name: "Complete Order" }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Complete order",
      }),
    );

    const alert = await screen.findByText(/no longer fully covered, so it was not completed/i);
    expect(alert).toBeInTheDocument();
    expect(
      screen.getByText("Purchased stock on hand no longer covers this order."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Complete Order" })).toBeInTheDocument();
  });

  it("shows the stale-version panel with Refresh on a version conflict", async () => {
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, READY_ORDER),
      },
      {
        method: "POST",
        pattern: /\/orders\/o1\/complete$/,
        respond: () =>
          jsonResponse(409, {
            error: { code: "STALE_VERSION", message: "This record changed.", issues: [] },
          }),
      },
    ]);
    renderOrder();
    fireEvent.click(await screen.findByRole("button", { name: "Complete Order" }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Complete order",
      }),
    );
    expect(await screen.findByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });

  it("shows other structured rejections (production locked) as a readable alert", async () => {
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, READY_ORDER),
      },
      {
        method: "POST",
        pattern: /\/orders\/o1\/complete$/,
        respond: () =>
          jsonResponse(409, {
            error: {
              code: "ORDER_PRODUCTION_LOCKED",
              message: "This order has demand currently covered by an active production run.",
              issues: [],
            },
          }),
      },
    ]);
    renderOrder();
    fireEvent.click(await screen.findByRole("button", { name: "Complete Order" }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Complete order",
      }),
    );
    expect(await screen.findByText(/active production run/i)).toBeInTheDocument();
  });

  it("disables Complete while an active production run covers the order's demand", async () => {
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, { ...READY_ORDER, production_locked: true }),
      },
    ]);
    renderOrder();
    expect(await screen.findByRole("button", { name: "Complete Order" })).toBeDisabled();
  });

  it("refreshes the dashboard, shopping list and inventory views after a successful completion", async () => {
    let completed = false;
    mockFetch([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, completed ? COMPLETED_ORDER : READY_ORDER),
      },
      {
        method: "POST",
        pattern: /\/orders\/o1\/complete$/,
        respond: () => {
          completed = true;
          return jsonResponse(200, COMPLETED_ORDER);
        },
      },
    ]);
    const client = renderOrder();
    const spy = vi.spyOn(client, "invalidateQueries");
    fireEvent.click(await screen.findByRole("button", { name: "Complete Order" }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Complete order",
      }),
    );
    await screen.findByTestId("completed-panel");
    const keys = spy.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
    for (const key of [
      "dashboard",
      "shopping-list",
      "surplus",
      "purchased-inventory",
      "production-requirements",
    ]) {
      expect(keys).toContain(JSON.stringify([key]));
    }
  });
});
