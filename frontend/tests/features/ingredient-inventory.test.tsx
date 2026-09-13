// Ingredient physical-inventory feature tests (Phase 5 Plan §G): detail-page display,
// Initial Balance/Restock unit normalization, Manual Adjustment, and the reconciliation
// attention state for a negative balance.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";

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

function selectOptionWithin(
  container: HTMLElement,
  triggerLabel: RegExp | string,
  optionText: RegExp | string,
) {
  fireEvent.click(within(container).getByLabelText(triggerLabel));
  const option = screen.getByText(optionText);
  fireEvent.pointerDown(option, { pointerId: 1, button: 0 });
  fireEvent.pointerUp(option, { pointerId: 1, button: 0 });
  fireEvent.click(option);
}

interface Handler {
  method: string;
  pattern: RegExp;
  respond: (url: string, init?: RequestInit) => Response;
}

function fetchRouterFor(handlers: Handler[]) {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.endsWith("/api/v1/auth/me") && method === "GET") {
      return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
    }
    const handler = handlers.find((h) => h.method === method && h.pattern.test(url));
    if (!handler) {
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    }
    return Promise.resolve(handler.respond(url, init));
  });
}

const FLOUR_BASE = {
  id: "i1",
  name: "Flour",
  measurement_family: "WEIGHT",
  canonical_unit: "g",
  is_active: true,
  version: 1,
  physical_quantity: "0.000000",
  weighted_average_unit_cost: "0.000000",
  latest_purchase_unit_cost: null,
  replacement_unit_cost: null,
  effective_replacement_cost: null,
};

describe("Ingredient detail — inventory display", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows physical quantity, costs, and a reconciliation badge for a negative balance", async () => {
    const NEGATIVE_FLOUR = {
      ...FLOUR_BASE,
      physical_quantity: "-15.000000",
      weighted_average_unit_cost: "2.500000",
      latest_purchase_unit_cost: "3.000000",
      effective_replacement_cost: "3.000000",
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, NEGATIVE_FLOUR) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });

    expect(screen.getByText("Needs reconciliation")).toBeInTheDocument();
    expect(screen.getByText(/-15 g/)).toBeInTheDocument();
    expect(screen.getByText("2.5")).toBeInTheDocument();
    expect(screen.getByText(/following latest purchase cost/i)).toBeInTheDocument();
  });

  it("does not show the reconciliation badge for a non-negative balance", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, FLOUR_BASE) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });
    expect(screen.queryByText("Needs reconciliation")).not.toBeInTheDocument();
    // Never-initialized -> the Initial Balance link is offered.
    expect(screen.getByRole("link", { name: /initial balance/i })).toBeInTheDocument();
  });

  it("offers Restock but not Initial Balance once inventory has values", async () => {
    const INITIALIZED = { ...FLOUR_BASE, physical_quantity: "10.000000", weighted_average_unit_cost: "1.000000" };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, INITIALIZED) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });
    expect(screen.queryByRole("link", { name: /initial balance/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^restock$/i })).toBeInTheDocument();
  });
});

describe("Ingredient Initial Balance", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("normalizes a compatible unit and submits the request, then navigates to the detail page", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, FLOUR_BASE) },
        {
          method: "POST",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/initial-balance$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(201, { ...FLOUR_BASE, physical_quantity: "5000.000000", version: 2 });
          },
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/initial-balance");
    await screen.findByRole("heading", { name: /initial balance/i });

    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "5" } });
    selectOptionWithin(document.body, /^unit$/i, /kg \(kilograms\)/);
    fireEvent.change(screen.getByLabelText(/unit cost/i), { target: { value: "2" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save initial balance/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toMatchObject({ version: 1, quantity: "5", unit: "kg", unit_cost: "2" });
    expect(await screen.findByRole("heading", { name: "Flour" })).toBeInTheDocument();
  });

  it("shows the already-initialized conflict message without crashing", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, FLOUR_BASE) },
        {
          method: "POST",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/initial-balance$/,
          respond: () =>
            jsonResponse(409, {
              error: {
                code: "INGREDIENT_INVENTORY_ALREADY_INITIALIZED",
                message: "This ingredient's inventory has already been initialized.",
                issues: [],
              },
            }),
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/initial-balance");
    await screen.findByRole("heading", { name: /initial balance/i });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "5" } });
    selectOptionWithin(document.body, /^unit$/i, /g \(grams\)/);
    fireEvent.change(screen.getByLabelText(/unit cost/i), { target: { value: "2" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save initial balance/i }));
    });

    expect(
      await screen.findByText(/already been initialized/i),
    ).toBeInTheDocument();
  });
});

describe("Ingredient Restock", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("submits a same-family unit restock with the current version", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    const INITIALIZED = { ...FLOUR_BASE, physical_quantity: "10.000000", weighted_average_unit_cost: "1.000000", version: 2 };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, INITIALIZED) },
        {
          method: "POST",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/restock$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, { ...INITIALIZED, physical_quantity: "15.000000", version: 3 });
          },
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/restock");
    await screen.findByRole("heading", { name: /restock/i });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "5" } });
    selectOptionWithin(document.body, /^unit$/i, /g \(grams\)/);
    fireEvent.change(screen.getByLabelText(/unit cost/i), { target: { value: "5" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save restock/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toMatchObject({ version: 2, quantity: "5", unit: "g", unit_cost: "5" });
  });

  it("shows a stale-version conflict with a Refresh action", async () => {
    let getCallCount = 0;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/ingredients\/i1$/,
          respond: () => {
            getCallCount += 1;
            return jsonResponse(200, { ...FLOUR_BASE, version: getCallCount === 1 ? 1 : 2 });
          },
        },
        {
          method: "POST",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/restock$/,
          respond: () =>
            jsonResponse(409, {
              error: {
                code: "STALE_VERSION",
                message: "This record changed since you opened it.",
                issues: [],
              },
            }),
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/restock");
    await screen.findByRole("heading", { name: /restock/i });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "5" } });
    selectOptionWithin(document.body, /^unit$/i, /g \(grams\)/);
    fireEvent.change(screen.getByLabelText(/unit cost/i), { target: { value: "5" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save restock/i }));
    });

    expect(await screen.findByText(/changed since you opened this page/i)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });
    await waitFor(() => expect(getCallCount).toBe(2));
  });
});

describe("Ingredient Replacement Cost", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the effective cost following Latest Purchase Cost when no override is set", async () => {
    const NO_OVERRIDE = {
      ...FLOUR_BASE,
      latest_purchase_unit_cost: "3.000000",
      replacement_unit_cost: null,
      effective_replacement_cost: "3.000000",
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, NO_OVERRIDE) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    expect(screen.getByText(/following latest purchase cost/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /clear override/i })).toBeDisabled();
  });

  it("shows the effective cost as manually set when an override exists", async () => {
    const OVERRIDDEN = {
      ...FLOUR_BASE,
      latest_purchase_unit_cost: "3.000000",
      replacement_unit_cost: "4.500000",
      effective_replacement_cost: "4.500000",
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, OVERRIDDEN) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    expect(screen.getByText(/manually set/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /clear override/i })).toBeEnabled();
  });

  it("submits a new override with the current version", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    let capturedMethod: string | undefined;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, FLOUR_BASE) },
        {
          method: "PUT",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/replacement-cost$/,
          respond: (_url, init) => {
            capturedMethod = init!.method;
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, {
              ...FLOUR_BASE,
              replacement_unit_cost: "4.500000",
              effective_replacement_cost: "4.500000",
              version: 2,
            });
          },
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    fireEvent.change(screen.getByLabelText(/replacement cost override/i), {
      target: { value: "4.5" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save override/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedMethod).toBe("PUT");
    expect(capturedBody).toMatchObject({ version: 1, replacement_unit_cost: "4.5" });
  });

  it("clears an existing override by sending an explicit null", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    const OVERRIDDEN = {
      ...FLOUR_BASE,
      replacement_unit_cost: "4.500000",
      effective_replacement_cost: "4.500000",
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, OVERRIDDEN) },
        {
          method: "PUT",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/replacement-cost$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, {
              ...FLOUR_BASE,
              replacement_unit_cost: null,
              effective_replacement_cost: FLOUR_BASE.latest_purchase_unit_cost,
              version: 2,
            });
          },
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /clear override/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toEqual({ version: 1, replacement_unit_cost: null });
  });

  it("shows a stale-version conflict with a Refresh action", async () => {
    let getCallCount = 0;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/ingredients\/i1$/,
          respond: () => {
            getCallCount += 1;
            return jsonResponse(200, { ...FLOUR_BASE, version: getCallCount === 1 ? 1 : 2 });
          },
        },
        {
          method: "PUT",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/replacement-cost$/,
          respond: () =>
            jsonResponse(409, {
              error: { code: "STALE_VERSION", message: "This record changed since you opened it.", issues: [] },
            }),
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    fireEvent.change(screen.getByLabelText(/replacement cost override/i), {
      target: { value: "4.5" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save override/i }));
    });

    expect(await screen.findByText(/changed since you opened this page/i)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });
    await waitFor(() => expect(getCallCount).toBe(2));
  });

  it("shows an explanation instead of the form when the ingredient is archived", async () => {
    const ARCHIVED = { ...FLOUR_BASE, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, ARCHIVED) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    expect(screen.getByText(/archived/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/replacement cost override/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back to ingredient/i })).toBeInTheDocument();
  });
});

describe("Ingredient action visibility for an archived resource", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("hides Initial Balance, Restock, and Replacement Cost, but keeps Adjust and History", async () => {
    const ARCHIVED = { ...FLOUR_BASE, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, ARCHIVED) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });
    expect(screen.queryByRole("link", { name: /initial balance/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^restock$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /replacement cost/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^adjust$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^history$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reactivate/i })).toBeInTheDocument();
  });

  it("shows an explanation instead of a usable form when navigating directly to Restock", async () => {
    const ARCHIVED = { ...FLOUR_BASE, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, ARCHIVED) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/restock");
    await screen.findByRole("heading", { name: /restock/i });
    expect(screen.getByText(/archived/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^quantity$/i)).not.toBeInTheDocument();
  });

  it("shows an explanation instead of a usable form when navigating directly to Initial Balance", async () => {
    const ARCHIVED = { ...FLOUR_BASE, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, ARCHIVED) },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/initial-balance");
    await screen.findByRole("heading", { name: /initial balance/i });
    expect(screen.getByText(/archived/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^quantity$/i)).not.toBeInTheDocument();
  });
});

describe("Ingredient Manual Adjustment", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("requires a reason, displays the canonical unit, and submits a negative quantity change", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/ingredients\/i1$/, respond: () => jsonResponse(200, FLOUR_BASE) },
        {
          method: "POST",
          pattern: /\/api\/v1\/ingredients\/i1\/inventory\/adjustments$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, { ...FLOUR_BASE, physical_quantity: "-5.000000", version: 2 });
          },
        },
      ]),
    );

    renderAt("/app/inventory/ingredients/i1/adjust");
    await screen.findByRole("heading", { name: /manual adjustment/i });
    // Canonical unit displayed beside the input (Phase 5 Plan approval decision 10).
    expect(screen.getByText(/quantity change \(g\)/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/quantity change/i), { target: { value: "-5" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save adjustment/i }));
    });
    expect(await screen.findByText(/reason is required/i)).toBeInTheDocument();
    expect(capturedBody).toBeUndefined();

    selectOptionWithin(document.body, /^reason$/i, /count correction/i);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save adjustment/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toMatchObject({
      version: 1,
      quantity_change: "-5",
      reason: "COUNT_CORRECTION",
    });
  });
});
