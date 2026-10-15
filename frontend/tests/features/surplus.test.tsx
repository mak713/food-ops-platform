// Surplus feature tests (SUR-001..003/008; Phase 8 Final Architecture Lock §9,
// corrected by Final Architecture Amendment §3 and Final Architecture Lock Addendum
// §B), following orders.test.tsx's exact template.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function jsonResponse(status: number, body: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
  });
}

const AUTHENTICATED_ME_RESPONSE = jsonResponse(200, {
  user: { id: "u1", name: "Test Owner", email: "owner@example.com" },
  business: { id: "b1", name: "Test Bakery", timezone: "America/New_York" },
});

interface Handler {
  method: string;
  pattern: RegExp;
  respond: (url: string, init?: RequestInit) => Response;
}

function mockFetchRouter(handlers: Handler[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/auth/me") && method === "GET") {
        return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
      }
      const handler = handlers.find((h) => h.method === method && h.pattern.test(url));
      if (!handler) {
        throw new Error(`Unhandled fetch: ${method} ${url}`);
      }
      return Promise.resolve(handler.respond(url, init));
    }),
  );
}

function renderAt(initialPath: string) {
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

async function selectOption(triggerLabel: RegExp, optionText: RegExp) {
  fireEvent.click(screen.getByLabelText(triggerLabel));
  const option = await screen.findByText(optionText);
  fireEvent.pointerDown(option, { pointerId: 1, button: 0 });
  fireEvent.pointerUp(option, { pointerId: 1, button: 0 });
  fireEvent.click(option);
}

const PRODUCED_PRODUCT = {
  id: "prod1",
  name: "Sourdough Loaf",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: true,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [],
};

const RUN_SOURCED_LOT = {
  id: "surplus1",
  product_id: "prod1",
  product_name: "Sourdough Loaf",
  source_production_run_id: "run1",
  physical_quantity: "6.000000",
  unit_cost_basis: "1.250000",
  source_total_cost_basis: "7.50",
  produced_at: "2026-02-01T12:00:00Z",
  usable_through_date: "2026-02-05",
  is_reusable: true,
  notes: null,
  version: 1,
};

const MANUAL_LOT = {
  ...RUN_SOURCED_LOT,
  id: "surplus2",
  source_production_run_id: null,
  source_total_cost_basis: null,
};

describe("Surplus list page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists lots with a Production/Manual source badge and action links", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/surplus\?/,
        respond: () =>
          jsonResponse(200, { items: [RUN_SOURCED_LOT, MANUAL_LOT], total: 2, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/surplus");
    await screen.findByRole("heading", { name: "Surplus" });

    expect(await screen.findAllByText("Sourdough Loaf")).toHaveLength(2);
    const table = screen.getByRole("table");
    expect(within(table).getByText("Production")).toBeInTheDocument();
    expect(within(table).getByText("Manual")).toBeInTheDocument();
    const adjustLinks = screen.getAllByRole("link", { name: "Adjust" });
    expect(adjustLinks[0]).toHaveAttribute("href", "/app/surplus/surplus1/adjust");
    const historyLinks = screen.getAllByRole("link", { name: "History" });
    expect(historyLinks[0]).toHaveAttribute("href", "/app/surplus/surplus1/history");
  });

  it("shows an empty state with no surplus recorded", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/surplus\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/surplus");
    await screen.findByRole("heading", { name: "Surplus" });
    expect(await screen.findByText(/no surplus recorded yet/i)).toBeInTheDocument();
  });
});

describe("Record Surplus (manual creation)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("submits a manual creation with the selected product and required cost basis", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [PRODUCED_PRODUCT], total: 1, limit: 200, offset: 0 }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/surplus$/,
        respond: (_url, init) => {
          createCallCount += 1;
          const body = JSON.parse(init?.body as string);
          expect(body).toMatchObject({
            product_id: "prod1",
            quantity: "5",
            unit_cost_basis: "1.50",
          });
          return jsonResponse(201, { ...MANUAL_LOT, physical_quantity: "5.000000", unit_cost_basis: "1.500000" });
        },
      },
    ]);

    renderAt("/app/surplus/new");
    await screen.findByRole("heading", { name: "Record Surplus" });

    await selectOption(/^product$/i, /sourdough loaf/i);
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText(/unit cost basis/i), { target: { value: "1.50" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Record Surplus" }));
    });

    await waitFor(() => expect(createCallCount).toBe(1));
  });

  it("requires a cost basis before submitting", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [PRODUCED_PRODUCT], total: 1, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/surplus/new");
    await screen.findByRole("heading", { name: "Record Surplus" });

    await selectOption(/^product$/i, /sourdough loaf/i);
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "5" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Record Surplus" }));
    });

    expect(await screen.findByText(/unit cost basis is required/i)).toBeInTheDocument();
  });
});

describe("Surplus adjustment (reduction-only)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("submits a within-slack reduction as a negative delta", async () => {
    let adjustCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/surplus\/surplus1$/,
        respond: () => jsonResponse(200, RUN_SOURCED_LOT),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/surplus\/surplus1\/adjust$/,
        respond: (_url, init) => {
          adjustCallCount += 1;
          const body = JSON.parse(init?.body as string);
          expect(body.quantity_delta).toBe("-2");
          expect(body.reason).toBe("SPOILAGE_OR_WASTE");
          return jsonResponse(200, { ...RUN_SOURCED_LOT, physical_quantity: "4.000000", version: 2 });
        },
      },
    ]);

    renderAt("/app/surplus/surplus1/adjust");
    await screen.findByRole("heading", { name: /adjust surplus — sourdough loaf/i });

    fireEvent.change(screen.getByLabelText(/quantity to remove/i), { target: { value: "2" } });
    await selectOption(/^reason$/i, /spoilage or waste/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save adjustment/i }));
    });

    await waitFor(() => expect(adjustCallCount).toBe(1));
  });

  it("shows the reconciliation warning and resubmits with the acknowledged fingerprint", async () => {
    let adjustCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/surplus\/surplus1$/,
        respond: () => jsonResponse(200, RUN_SOURCED_LOT),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/surplus\/surplus1\/adjust$/,
        respond: (_url, init) => {
          adjustCallCount += 1;
          const body = JSON.parse(init?.body as string);
          if (!body.acknowledged_warning_fingerprint) {
            return jsonResponse(409, {
              error: {
                code: "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT",
                message: "1 Ready order(s) will be demoted to Confirmed.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION",
                    message: "1 Ready order(s) will be demoted to Confirmed.",
                    field: null,
                    resource: "surplus_inventory",
                    details: {
                      demoted_order_ids: ["order1"],
                      fingerprint: "SURPLUS_ADJUST:surplus1:4:order1",
                    },
                  },
                ],
              },
            });
          }
          expect(body.acknowledged_warning_fingerprint).toBe("SURPLUS_ADJUST:surplus1:4:order1");
          return jsonResponse(200, { ...RUN_SOURCED_LOT, physical_quantity: "4.000000", version: 2 });
        },
      },
    ]);

    renderAt("/app/surplus/surplus1/adjust");
    await screen.findByRole("heading", { name: /adjust surplus — sourdough loaf/i });

    fireEvent.change(screen.getByLabelText(/quantity to remove/i), { target: { value: "2" } });
    await selectOption(/^reason$/i, /count correction/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save adjustment/i }));
    });

    expect(
      await screen.findByText(/1 ready order\(s\) will be demoted to confirmed/i),
    ).toBeInTheDocument();
    const adjustAnyway = screen.getByRole("button", { name: /adjust anyway/i });

    await act(async () => {
      fireEvent.click(adjustAnyway);
    });

    await waitFor(() => expect(adjustCallCount).toBe(2));
  });
});

describe("Surplus transaction history", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists transactions for a lot", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/surplus\/surplus1$/,
        respond: () => jsonResponse(200, RUN_SOURCED_LOT),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/surplus\/surplus1\/transactions\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "txn1",
                surplus_inventory_id: "surplus1",
                transaction_type: "PRODUCTION_OUTPUT",
                quantity_change: "8.000000",
                reason: null,
                notes: null,
                order_id: null,
                production_run_id: "run1",
                created_at: "2026-02-01T12:00:00Z",
              },
              {
                id: "txn2",
                surplus_inventory_id: "surplus1",
                transaction_type: "MANUAL_ADJUSTMENT",
                quantity_change: "-2.000000",
                reason: "SPOILAGE_OR_WASTE",
                notes: "Dropped a tray",
                order_id: null,
                production_run_id: null,
                created_at: "2026-02-02T08:00:00Z",
              },
            ],
            total: 2,
            limit: 200,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/surplus/surplus1/history");
    await screen.findByRole("heading", { name: /surplus history — sourdough loaf/i });

    expect(await screen.findByText("Production output")).toBeInTheDocument();
    expect(screen.getByText("Manual adjustment")).toBeInTheDocument();
    expect(screen.getByText("SPOILAGE_OR_WASTE")).toBeInTheDocument();
    expect(screen.getByText("Dropped a tray")).toBeInTheDocument();
  });
});
