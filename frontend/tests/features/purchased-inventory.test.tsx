// Purchased Product Inventory feature tests (Phase 5 Plan §G): Product-detail
// conditional display, first-ever Restock, Initial Balance, Manual Adjustment, the
// top-level Inventory-module list, and the Inventory hub navigation.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const PURCHASED_PRODUCT = {
  id: "p1",
  name: "Canned Tomatoes",
  description: null,
  product_type: "PURCHASED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [],
};

const PRODUCED_PRODUCT = { ...PURCHASED_PRODUCT, id: "p2", product_type: "PRODUCED" };

const NOT_FOUND_404 = jsonResponse(404, {
  error: { code: "PURCHASED_INVENTORY_NOT_INITIALIZED", message: "Not initialized yet.", issues: [] },
});

describe("Product Detail — Purchased Inventory section", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the Purchased Inventory section with an Initial Balance/Restock offer when nothing is recorded yet", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/, respond: () => NOT_FOUND_404.clone() },
      ]),
    );

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Canned Tomatoes" });
    expect(await screen.findByText("Purchased Inventory")).toBeInTheDocument();
    expect(screen.getByText(/no inventory recorded yet/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /record initial balance/i })).toBeInTheDocument();
  });

  it("does not show a Purchased Inventory section, and never queries it, for a PRODUCED product", async () => {
    const fetchMock = fetchRouterFor([
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/products/p2");
    await screen.findByRole("heading", { name: "Canned Tomatoes" });
    expect(screen.queryByText("Purchased Inventory")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("purchased-inventory"))).toBe(
      false,
    );
  });

  it("shows the recorded balance and effective replacement cost once initialized", async () => {
    const INVENTORY = {
      product_id: "p1",
      physical_quantity: "20.000000",
      weighted_average_unit_cost: "1.500000",
      latest_purchase_unit_cost: "2.250000",
      replacement_unit_cost: null,
      effective_replacement_cost: "2.250000",
      version: 1,
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/, respond: () => jsonResponse(200, INVENTORY) },
      ]),
    );

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Canned Tomatoes" });
    expect(await screen.findByText("20")).toBeInTheDocument();
    expect(screen.getByText("1.5")).toBeInTheDocument();
    expect(screen.getAllByText(/2\.25/).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /^restock$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^adjust$/i })).toBeInTheDocument();
  });
});

describe("Purchased Product first-ever Restock", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("omits version when no inventory row exists yet, and the product becomes visible afterward", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/, respond: () => NOT_FOUND_404.clone() },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory\/restock$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, {
              product_id: "p1",
              physical_quantity: "10.000000",
              weighted_average_unit_cost: "3.000000",
              latest_purchase_unit_cost: "3.000000",
              replacement_unit_cost: null,
              effective_replacement_cost: "3.000000",
              version: 1,
            });
          },
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/restock");
    await screen.findByRole("heading", { name: /restock/i });
    expect(screen.getByText(/this restock will be its first/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/unit cost/i), { target: { value: "3" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save restock/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toMatchObject({ quantity: "10", unit_cost: "3" });
    expect(capturedBody?.version).toBeUndefined();
    expect(await screen.findByRole("heading", { name: "Canned Tomatoes" })).toBeInTheDocument();
  });
});

describe("Purchased Product Manual Adjustment", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("rejects an adjustment that would go below zero with a clean message", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/,
          respond: () =>
            jsonResponse(200, {
              product_id: "p1",
              physical_quantity: "5.000000",
              weighted_average_unit_cost: "2.000000",
              latest_purchase_unit_cost: "2.000000",
              replacement_unit_cost: null,
              effective_replacement_cost: "2.000000",
              version: 1,
            }),
        },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory\/adjustments$/,
          respond: () =>
            jsonResponse(422, {
              error: {
                code: "PURCHASED_INVENTORY_INSUFFICIENT_BALANCE",
                message: "This adjustment would make the purchased inventory balance negative.",
                issues: [],
              },
            }),
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/adjust");
    await screen.findByRole("heading", { name: /manual adjustment/i });
    fireEvent.change(screen.getByLabelText(/quantity change/i), { target: { value: "-10" } });
    fireEvent.click(screen.getByLabelText(/^reason$/i));
    const option = screen.getByText(/count correction/i);
    fireEvent.pointerDown(option, { pointerId: 1, button: 0 });
    fireEvent.pointerUp(option, { pointerId: 1, button: 0 });
    fireEvent.click(option);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save adjustment/i }));
    });

    expect(await screen.findByText(/would make the purchased inventory balance negative/i)).toBeInTheDocument();
  });
});

describe("Purchased Product Replacement Cost", () => {
  afterEach(() => vi.unstubAllGlobals());

  const BASE_INVENTORY = {
    product_id: "p1",
    physical_quantity: "20.000000",
    weighted_average_unit_cost: "1.500000",
    latest_purchase_unit_cost: "2.250000",
    replacement_unit_cost: null,
    effective_replacement_cost: "2.250000",
    version: 1,
  };

  it("shows the effective cost following Latest Purchase Cost when no override is set", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/,
          respond: () => jsonResponse(200, BASE_INVENTORY),
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    expect(screen.getByText(/following latest purchase cost/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /clear override/i })).toBeDisabled();
  });

  it("shows the effective cost as manually set when an override exists", async () => {
    const OVERRIDDEN = { ...BASE_INVENTORY, replacement_unit_cost: "5.000000", effective_replacement_cost: "5.000000" };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/,
          respond: () => jsonResponse(200, OVERRIDDEN),
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/replacement-cost");
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
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/,
          respond: () => jsonResponse(200, BASE_INVENTORY),
        },
        {
          method: "PUT",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory\/replacement-cost$/,
          respond: (_url, init) => {
            capturedMethod = init!.method;
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, {
              ...BASE_INVENTORY,
              replacement_unit_cost: "5.000000",
              effective_replacement_cost: "5.000000",
              version: 2,
            });
          },
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    fireEvent.change(screen.getByLabelText(/replacement cost override/i), {
      target: { value: "5" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save override/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedMethod).toBe("PUT");
    expect(capturedBody).toMatchObject({ version: 1, replacement_unit_cost: "5" });
  });

  it("clears an existing override by sending an explicit null", async () => {
    let capturedBody: Record<string, unknown> | undefined;
    const OVERRIDDEN = { ...BASE_INVENTORY, replacement_unit_cost: "5.000000", effective_replacement_cost: "5.000000" };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/,
          respond: () => jsonResponse(200, OVERRIDDEN),
        },
        {
          method: "PUT",
          pattern: /\/api\/v1\/products\/p1\/purchased-inventory\/replacement-cost$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(200, {
              ...BASE_INVENTORY,
              replacement_unit_cost: null,
              effective_replacement_cost: BASE_INVENTORY.latest_purchase_unit_cost,
              version: 2,
            });
          },
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /clear override/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toEqual({ version: 1, replacement_unit_cost: null });
  });

  it("shows an explanation instead of the form when the product is inactive", async () => {
    const INACTIVE_PRODUCT = { ...PURCHASED_PRODUCT, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, INACTIVE_PRODUCT) },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/replacement-cost");
    await screen.findByRole("heading", { name: /replacement cost/i });
    expect(screen.getByText(/inactive/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/replacement cost override/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back to product/i })).toBeInTheDocument();
  });

  it("shows a no-inventory-yet message when the product is active but never initialized", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/, respond: () => NOT_FOUND_404.clone() },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/replacement-cost");
    await screen.findByRole("heading", { name: /no inventory recorded yet/i });
    expect(screen.queryByLabelText(/replacement cost override/i)).not.toBeInTheDocument();
  });
});

describe("Purchased Product action visibility for an inactive resource", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("hides Restock and Replacement Cost, but keeps Adjust and View History", async () => {
    const INACTIVE_PRODUCT = { ...PURCHASED_PRODUCT, is_active: false };
    const INVENTORY = {
      product_id: "p1",
      physical_quantity: "20.000000",
      weighted_average_unit_cost: "1.500000",
      latest_purchase_unit_cost: "2.250000",
      replacement_unit_cost: null,
      effective_replacement_cost: "2.250000",
      version: 1,
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, INACTIVE_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/, respond: () => jsonResponse(200, INVENTORY) },
      ]),
    );

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Canned Tomatoes" });
    await screen.findByText("Purchased Inventory");
    expect(await screen.findByRole("link", { name: /^adjust$/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^restock$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /replacement cost/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view history/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reactivate/i })).toBeInTheDocument();
  });

  it("shows an explanation instead of a usable form when navigating directly to Restock", async () => {
    const INACTIVE_PRODUCT = { ...PURCHASED_PRODUCT, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, INACTIVE_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/purchased-inventory$/, respond: () => NOT_FOUND_404.clone() },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/restock");
    await screen.findByRole("heading", { name: /restock/i });
    expect(screen.getByText(/inactive/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^quantity$/i)).not.toBeInTheDocument();
  });

  it("shows an explanation instead of a usable form when navigating directly to Initial Balance", async () => {
    const INACTIVE_PRODUCT = { ...PURCHASED_PRODUCT, is_active: false };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, INACTIVE_PRODUCT) },
      ]),
    );

    renderAt("/app/inventory/purchased-products/p1/initial-balance");
    await screen.findByRole("heading", { name: /initial balance/i });
    expect(screen.getByText(/inactive/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^quantity$/i)).not.toBeInTheDocument();
  });
});

describe("Top-level Purchased Product Inventory list", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows initialized and not-yet-initialized rows", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/purchased-inventory\?/,
          respond: () =>
            jsonResponse(200, {
              items: [
                {
                  product_id: "p1",
                  product_name: "Canned Tomatoes",
                  product_is_active: true,
                  physical_quantity: "10.000000",
                  weighted_average_unit_cost: "2.000000",
                  latest_purchase_unit_cost: "2.000000",
                  replacement_unit_cost: null,
                  effective_replacement_cost: "2.000000",
                  version: 1,
                },
                {
                  product_id: "p3",
                  product_name: "Jarred Olives",
                  product_is_active: true,
                  physical_quantity: null,
                  weighted_average_unit_cost: null,
                  latest_purchase_unit_cost: null,
                  replacement_unit_cost: null,
                  effective_replacement_cost: null,
                  version: null,
                },
              ],
              total: 2,
              limit: 50,
              offset: 0,
            }),
        },
      ]),
    );

    renderAt("/app/inventory/purchased-products");
    await screen.findByRole("heading", { name: /purchased product inventory/i });
    expect(await screen.findByText("Canned Tomatoes")).toBeInTheDocument();
    expect(screen.getByText("Jarred Olives")).toBeInTheDocument();
    expect(screen.getByText("Not initialized")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });
});

describe("Inventory hub", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("links to Ingredients, Purchased Products, and Inventory History", async () => {
    vi.stubGlobal("fetch", fetchRouterFor([]));
    renderAt("/app/inventory");
    await screen.findByRole("heading", { name: /^inventory$/i });
    expect(screen.getByRole("link", { name: /ingredients/i })).toHaveAttribute(
      "href",
      "/app/inventory/ingredients",
    );
    expect(screen.getByRole("link", { name: /purchased products/i })).toHaveAttribute(
      "href",
      "/app/inventory/purchased-products",
    );
    expect(screen.getByRole("link", { name: /inventory history/i })).toHaveAttribute(
      "href",
      "/app/inventory/history",
    );
  });
});

describe("Inventory History — resource picker", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("displays the selected ingredient's human-readable name (never its raw ID) in the closed picker", async () => {
    // Regression: caught during manual browser verification — the picker's SelectValue
    // originally rendered the raw selected `value` (the ingredient's UUID) with no
    // children render-function, exactly the bug already fixed for Recipe's ingredient
    // selector in Phase 4. A UUID-shaped id makes the raw-value fallback unmistakable.
    const UUID_INGREDIENT = {
      id: "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6",
      name: "Vanilla Extract",
      measurement_family: "WEIGHT",
      canonical_unit: "g",
      is_active: true,
      version: 1,
      physical_quantity: "0.000000",
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/ingredients\?/,
          respond: () =>
            jsonResponse(200, { items: [UUID_INGREDIENT], total: 1, limit: 200, offset: 0 }),
        },
      ]),
    );

    renderAt("/app/inventory/history");
    await screen.findByRole("heading", { name: /inventory history/i });

    const trigger = screen.getByLabelText(/choose an ingredient/i);
    fireEvent.click(trigger);
    const option = await screen.findByText("Vanilla Extract");
    fireEvent.pointerDown(option, { pointerId: 1, button: 0 });
    fireEvent.pointerUp(option, { pointerId: 1, button: 0 });
    fireEvent.click(option);

    expect(await screen.findByLabelText(/choose an ingredient/i)).toHaveTextContent(
      "Vanilla Extract",
    );
    expect(screen.queryByText(UUID_INGREDIENT.id)).not.toBeInTheDocument();
  });
});
