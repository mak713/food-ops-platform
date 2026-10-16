// Production feature tests (Phase 8 Final Architecture Lock §5/§6/§12), following
// orders.test.tsx's exact template (mockFetchRouter, createMemoryRouter, real
// QueryClientProvider/AuthProvider).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const REQUIREMENT = {
  id: "req1",
  product_id: "prod1",
  product_name: "Sourdough Loaf",
  recipe_revision_id: "rev1",
  demand_date: "2026-02-01",
  calculation_status: "CALCULATED",
  confirmed_demand_quantity: "24.000000",
  surplus_allocated_quantity: "0.000000",
  production_demand_quantity: "24.000000",
  recommended_batches: 2,
  expected_output_quantity: "24.000000",
  expected_excess_quantity: "0.000000",
  estimated_active_minutes: 60,
  estimated_elapsed_minutes: 120,
  suggested_start_at: "2026-01-31T18:00:00Z",
  estimated_ingredient_cost: "10.00",
  estimated_labor_cost: "15.00",
  estimated_direct_production_cost: "25.00",
  has_shortage: false,
  ingredient_needs: [],
  affected_orders: [],
};

const RUN_INGREDIENT = {
  ingredient_id: "ing1",
  ingredient_name: "Flour",
  planned_quantity: "4000.000000",
  actual_quantity: null,
  weighted_average_unit_cost_snapshot: "0.020000",
  replacement_unit_cost_snapshot: null,
  cost_basis_source: "WAC",
  actual_unit_cost_basis: null,
  actual_total_cost: null,
};

const RUN = {
  id: "run1",
  business_id: "b1",
  source_production_requirement_id: "req1",
  product_id: "prod1",
  product_name: "Sourdough Loaf",
  recipe_revision_id: "rev1",
  status: "IN_PRODUCTION",
  planned_batches: 2,
  planned_output_quantity: "24.000000",
  actual_output_quantity: null,
  recipe_yield_snapshot: "12.000000",
  active_minutes_per_batch_snapshot: 30,
  elapsed_minutes_per_batch_snapshot: 60,
  labor_rate_snapshot: "20.00",
  estimated_ingredient_cost: "10.00",
  estimated_labor_cost: "15.00",
  estimated_total_production_cost: "25.00",
  actual_ingredient_cost: null,
  actual_labor_cost: null,
  actual_total_production_cost: null,
  started_at: "2026-01-31T18:00:00Z",
  completed_at: null,
  canceled_at: null,
  version: 1,
  run_ingredients: [RUN_INGREDIENT],
};

describe("Production Plan page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists actionable requirements and links to Start", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/requirements\?/,
        respond: () => jsonResponse(200, { items: [REQUIREMENT], total: 1, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/production");
    await screen.findByRole("heading", { name: "Production Plan" });

    expect(await screen.findByText("Sourdough Loaf")).toBeInTheDocument();
    expect(screen.getByText("2026-02-01")).toBeInTheDocument();
    const startLink = screen.getByRole("link", { name: /review & start/i });
    expect(startLink).toHaveAttribute("href", "/app/production/start/req1");
  });

  it("shows an empty state when nothing needs production", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/requirements\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/production");
    await screen.findByRole("heading", { name: "Production Plan" });
    expect(await screen.findByText(/nothing needs production right now/i)).toBeInTheDocument();
  });
});

describe("Start Production page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("starts production with the recommended batch count", async () => {
    let startCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/requirements\/req1$/,
        respond: () => jsonResponse(200, REQUIREMENT),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/production-runs$/,
        respond: (_url, init) => {
          startCallCount += 1;
          const body = JSON.parse(init?.body as string);
          expect(body.production_requirement_id).toBe("req1");
          expect(body.batches).toBeNull();
          return jsonResponse(201, RUN);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/run1$/,
        respond: () => jsonResponse(200, RUN),
      },
    ]);

    renderAt("/app/production/start/req1");
    await screen.findByRole("heading", { name: /start production — sourdough loaf/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start Production" }));
    });

    await waitFor(() => expect(startCallCount).toBe(1));
    expect(await screen.findByText(/started/i)).toBeInTheDocument();
  });

  it("shows the underproduction warning and resubmits with the acknowledged fingerprint", async () => {
    let startCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/requirements\/req1$/,
        respond: () => jsonResponse(200, REQUIREMENT),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/production-runs$/,
        respond: (_url, init) => {
          startCallCount += 1;
          const body = JSON.parse(init?.body as string);
          if (!body.acknowledged_warning_fingerprint) {
            return jsonResponse(409, {
              error: {
                code: "UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED",
                message: "This batch count will not fully cover outstanding demand.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "UNDERPRODUCTION",
                    message: "Producing 12 will leave 12 units of demand uncovered.",
                    field: null,
                    resource: "production_requirement",
                    details: { fingerprint: "UNDERPRODUCTION:req1:1:12:12" },
                  },
                ],
              },
            });
          }
          expect(body.acknowledged_warning_fingerprint).toBe("UNDERPRODUCTION:req1:1:12:12");
          return jsonResponse(201, RUN);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/run1$/,
        respond: () => jsonResponse(200, RUN),
      },
    ]);

    renderAt("/app/production/start/req1");
    await screen.findByRole("heading", { name: /start production — sourdough loaf/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start Production" }));
    });

    expect(
      await screen.findByText(/will leave 12 units of demand uncovered/i),
    ).toBeInTheDocument();
    const startAnyway = screen.getByRole("button", { name: /start anyway/i });

    await act(async () => {
      fireEvent.click(startAnyway);
    });

    await waitFor(() => expect(startCallCount).toBe(2));
  });

  it("shows the cost-basis-unknown error with no acknowledgment path", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/requirements\/req1$/,
        respond: () => jsonResponse(200, REQUIREMENT),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/production-runs$/,
        respond: () =>
          jsonResponse(409, {
            error: {
              code: "PRODUCTION_COST_BASIS_UNKNOWN",
              message: "One or more ingredients have no resolvable cost basis.",
              issues: [
                {
                  severity: "ERROR",
                  code: "PRODUCTION_COST_BASIS_UNKNOWN",
                  message: "Flour has no weighted-average cost, replacement cost, or purchase history.",
                  field: null,
                  resource: "ingredient",
                  details: {},
                },
              ],
            },
          }),
      },
    ]);

    renderAt("/app/production/start/req1");
    await screen.findByRole("heading", { name: /start production — sourdough loaf/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start Production" }));
    });

    expect(
      await screen.findByText(/flour has no weighted-average cost/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /start anyway/i })).not.toBeInTheDocument();
  });
});

describe("Active Runs page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists runs and links to the run detail page", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\?/,
        respond: () => jsonResponse(200, { items: [RUN], total: 1, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/production/runs");
    await screen.findByRole("heading", { name: "Active Runs" });

    expect(await screen.findByText("Sourdough Loaf")).toBeInTheDocument();
    expect(screen.getByText("In Production")).toBeInTheDocument();
    const viewLink = screen.getByRole("link", { name: /view/i });
    expect(viewLink).toHaveAttribute("href", "/app/production/runs/run1");
  });
});

describe("Production Run detail page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("finishes a run and shows a clean-finish message", async () => {
    let finishCallCount = 0;
    let finished = false;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/run1$/,
        respond: () =>
          jsonResponse(200, finished ? { ...RUN, status: "COMPLETED", actual_output_quantity: "24.000000" } : RUN),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/production-runs\/run1\/finish$/,
        respond: (_url, init) => {
          finishCallCount += 1;
          const body = JSON.parse(init?.body as string);
          expect(body.actual_output_quantity).toBe("24");
          finished = true;
          return jsonResponse(200, {
            run: { ...RUN, status: "COMPLETED", actual_output_quantity: "24.000000" },
            negative_inventory_ingredient_ids: [],
            is_shortfall: false,
          });
        },
      },
    ]);

    renderAt("/app/production/runs/run1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    fireEvent.change(screen.getByLabelText(/actual usable output/i), { target: { value: "24" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Finish Production" }));
    });

    await waitFor(() => expect(finishCallCount).toBe(1));
    expect(
      await screen.findByText(/finished cleanly — demand fully covered/i),
    ).toBeInTheDocument();
  });

  it("shows shortfall and negative-inventory messaging on a partial finish", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/run1$/,
        respond: () => jsonResponse(200, RUN),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/production-runs\/run1\/finish$/,
        respond: () =>
          jsonResponse(200, {
            run: { ...RUN, status: "COMPLETED", actual_output_quantity: "6.000000" },
            negative_inventory_ingredient_ids: ["ing1"],
            is_shortfall: true,
          }),
      },
    ]);

    renderAt("/app/production/runs/run1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    fireEvent.change(screen.getByLabelText(/actual usable output/i), { target: { value: "6" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Finish Production" }));
    });

    expect(
      await screen.findByText(/did not fully cover its outstanding demand/i),
    ).toBeInTheDocument();
    expect(await screen.findByText(/flour/i, { selector: "p" })).toBeInTheDocument();
  });

  it("cancels an in-production run via the confirmation dialog", async () => {
    let cancelCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/production-runs\/run1$/,
        respond: () => jsonResponse(200, RUN),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/production-runs\/run1\/cancel$/,
        respond: () => {
          cancelCallCount += 1;
          return jsonResponse(200, { ...RUN, status: "CANCELED" });
        },
      },
    ]);

    renderAt("/app/production/runs/run1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel Run" }));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    });

    await waitFor(() => expect(cancelCallCount).toBe(1));
  });
});
