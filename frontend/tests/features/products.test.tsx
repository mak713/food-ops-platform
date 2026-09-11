// Product/SellingOption feature tests (Phase 3 plan v3 §17), following the App.test.tsx
// pattern.

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

interface Handler {
  method: string;
  pattern: RegExp;
  respond: (url: string) => Response;
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
      return Promise.resolve(handler.respond(url));
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

const PRODUCT_NO_OPTIONS = {
  id: "p1",
  name: "Sourdough Loaf",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [],
};

const SIX_PACK_OPTION = {
  id: "so1",
  product_id: "p1",
  name: "6-pack",
  quantity_units: "6.000000",
  price: "12.00",
  packaging_cost: "0.00",
  sort_order: 0,
  is_active: true,
  version: 1,
};

const PRODUCT_WITH_OPTION = { ...PRODUCT_NO_OPTIONS, selling_options: [SIX_PACK_OPTION] };

describe("Product list", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the empty state when there are no products", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 50, offset: 0 }),
      },
    ]);

    renderAt("/app/products");
    expect(await screen.findByText(/no products yet/i)).toBeInTheDocument();
  });

  it("renders products returned by the list endpoint", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1 }],
            total: 1,
            limit: 50,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/products");
    expect(await screen.findByText("Sourdough Loaf")).toBeInTheDocument();
  });
});

describe("Product create form", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a validation error for a blank name and never calls the API", async () => {
    mockFetchRouter([]);
    renderAt("/app/products/new");

    await screen.findByRole("heading", { name: /new product/i });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save/i }));
    });

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
  });

  it("creates a Product with no nested selling_options field and redirects to its detail page", async () => {
    let capturedBody: unknown;
    mockFetchRouter([
      {
        method: "POST",
        pattern: /\/api\/v1\/products$/,
        respond: () => jsonResponse(201, PRODUCT_NO_OPTIONS),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 50, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_NO_OPTIONS),
      },
    ]);
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/auth/me")) return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
      if (method === "POST" && url.endsWith("/api/v1/products")) {
        capturedBody = JSON.parse(init!.body as string);
        return Promise.resolve(jsonResponse(201, PRODUCT_NO_OPTIONS));
      }
      if (method === "GET" && /\/api\/v1\/products\/p1$/.test(url)) {
        return Promise.resolve(jsonResponse(200, PRODUCT_NO_OPTIONS));
      }
      if (method === "GET" && /\/api\/v1\/products\?/.test(url)) {
        return Promise.resolve(jsonResponse(200, { items: [], total: 0, limit: 50, offset: 0 }));
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/products/new");
    await screen.findByRole("heading", { name: /new product/i });
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "Sourdough Loaf" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    // Redirected to Product Detail — the empty Selling Options state is the normal
    // post-creation state (Phase 3 plan v3 §9), not an edge case.
    expect(await screen.findByRole("heading", { name: "Sourdough Loaf" })).toBeInTheDocument();
    expect(await screen.findByText(/no selling options yet/i)).toBeInTheDocument();

    // The create request itself never carries a selling_options field.
    expect(capturedBody).not.toHaveProperty("selling_options");
  });
});

describe("Product edit form — prerequisite-GET failure handling (Checkpoint 3 remediation)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a not-found state and never calls create when the prerequisite GET 404s", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/missing-id$/,
        respond: () =>
          jsonResponse(404, {
            error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
          }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/products$/,
        respond: () => {
          createCallCount += 1;
          return jsonResponse(201, PRODUCT_NO_OPTIONS);
        },
      },
    ]);

    renderAt("/app/products/missing-id/edit");

    expect(await screen.findByRole("heading", { name: /product not found/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(createCallCount).toBe(0);
  });

  it("shows an error banner with retry when the prerequisite GET fails with a server error, and never calls create", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/broken-id$/,
        respond: () =>
          jsonResponse(500, {
            error: { code: "INTERNAL_SERVER_ERROR", message: "Server error.", issues: [] },
          }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/products$/,
        respond: () => {
          createCallCount += 1;
          return jsonResponse(201, PRODUCT_NO_OPTIONS);
        },
      },
    ]);

    renderAt("/app/products/broken-id/edit");

    expect(await screen.findByRole("alert")).toHaveTextContent("Server error.");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(createCallCount).toBe(0);
  });

  it("recovers from a stale-version conflict via Refresh, discarding the stale edit", async () => {
    let patchCallCount = 0;
    let getCallCount = 0;
    const FRESH_PRODUCT = { ...PRODUCT_NO_OPTIONS, name: "Renamed elsewhere", version: 2 };

    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(200, getCallCount === 1 ? PRODUCT_NO_OPTIONS : FRESH_PRODUCT);
        },
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => {
          patchCallCount += 1;
          return jsonResponse(409, {
            error: {
              code: "STALE_VERSION",
              message:
                "This record changed since you opened it. Refresh the latest version and review your changes before saving again.",
              issues: [],
            },
          });
        },
      },
    ]);

    renderAt("/app/products/p1/edit");
    await screen.findByRole("heading", { name: /edit product/i });
    expect(await screen.findByLabelText(/^name$/i)).toHaveValue("Sourdough Loaf");

    fireEvent.change(screen.getByLabelText(/^description$/i), { target: { value: "New desc" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();
    expect(patchCallCount).toBe(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });

    await waitFor(() =>
      expect(screen.getByLabelText(/^name$/i)).toHaveValue(FRESH_PRODUCT.name),
    );
    expect(screen.queryByText(/changed since you opened it/i)).not.toBeInTheDocument();
    expect(getCallCount).toBe(2);
  });
});

describe("SellingOption validation and inline mutations (Checkpoint 3 remediation)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("blocks quantity 0 and price -1 client-side, before any fetch to create the option", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_NO_OPTIONS),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/products\/p1\/selling-options$/,
        respond: () => {
          createCallCount += 1;
          return jsonResponse(201, SIX_PACK_OPTION);
        },
      },
    ]);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText(/^price$/i), { target: { value: "-1" } });
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "Bad Option" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add selling option/i }));
    });

    expect(await screen.findByText(/quantity must be greater than 0/i)).toBeInTheDocument();
    expect(screen.getByText(/price must be 0 or greater/i)).toBeInTheDocument();
    expect(createCallCount).toBe(0);
  });

  it("recovers from a stale-version conflict on SellingOption archive via Refresh", async () => {
    let archiveCallCount = 0;
    let getCallCount = 0;
    const REACTIVATED_ELSEWHERE = { ...SIX_PACK_OPTION, version: 2 };

    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(
            200,
            getCallCount === 1
              ? PRODUCT_WITH_OPTION
              : { ...PRODUCT_WITH_OPTION, selling_options: [REACTIVATED_ELSEWHERE] },
          );
        },
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/products\/p1\/selling-options\/so1\/archive$/,
        respond: () => {
          archiveCallCount += 1;
          return jsonResponse(409, {
            error: {
              code: "STALE_VERSION",
              message: "This record changed since you opened it.",
              issues: [],
            },
          });
        },
      },
    ]);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    const optionRow = (await screen.findByText("6-pack")).closest("tr");
    if (!optionRow) throw new Error("expected to find the selling option's table row");

    await act(async () => {
      fireEvent.click(within(optionRow).getByRole("button", { name: /^archive$/i }));
    });

    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();
    expect(archiveCallCount).toBe(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });

    await waitFor(() => expect(getCallCount).toBe(2));
    expect(screen.queryByText(/changed since you opened it/i)).not.toBeInTheDocument();
  });

  it("resets the inline edit form to authoritative data after a stale-version Refresh, not the rejected local edit (manual-testing remediation)", async () => {
    // react-hook-form's `defaultValues` are only read once, at mount — this row's
    // component instance is never remounted just because its `option` prop changes (same
    // `key`). Without an explicit `reset()` after Refresh, re-opening the inline edit form
    // would keep showing whatever the user had locally typed before the rejected save.
    let patchCallCount = 0;
    let getCallCount = 0;
    const FRESH_OPTION = { ...SIX_PACK_OPTION, name: "6-pack (updated elsewhere)", version: 2 };

    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(
            200,
            getCallCount === 1
              ? PRODUCT_WITH_OPTION
              : { ...PRODUCT_WITH_OPTION, selling_options: [FRESH_OPTION] },
          );
        },
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/products\/p1\/selling-options\/so1$/,
        respond: () => {
          patchCallCount += 1;
          return jsonResponse(409, {
            error: {
              code: "STALE_VERSION",
              message: "This record changed since you opened it.",
              issues: [],
            },
          });
        },
      },
    ]);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    const optionRow = (await screen.findByText("6-pack")).closest("tr");
    if (!optionRow) throw new Error("expected to find the selling option's table row");

    await act(async () => {
      fireEvent.click(within(optionRow).getByRole("button", { name: /^edit$/i }));
    });
    fireEvent.change(within(optionRow).getByLabelText(/^name$/i), {
      target: { value: "Locally edited, never saved" },
    });
    await act(async () => {
      fireEvent.click(within(optionRow).getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();
    expect(patchCallCount).toBe(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });
    await waitFor(() => expect(getCallCount).toBe(2));

    // Refresh closes the inline form back to view mode showing the authoritative value —
    // the rejected local edit must not remain presented as current anywhere on the page.
    expect(await screen.findByText("6-pack (updated elsewhere)")).toBeInTheDocument();
    expect(screen.queryByText("Locally edited, never saved")).not.toBeInTheDocument();

    // Re-opening Edit must show the fresh authoritative value, not the stale local one.
    const refreshedRow = screen.getByText("6-pack (updated elsewhere)").closest("tr");
    if (!refreshedRow) throw new Error("expected to find the refreshed row");
    await act(async () => {
      fireEvent.click(within(refreshedRow).getByRole("button", { name: /^edit$/i }));
    });
    expect(within(refreshedRow).getByLabelText(/^name$/i)).toHaveValue(
      "6-pack (updated elsewhere)",
    );
  });
});

describe("SellingOptionRow — edit-session version capture (final pre-freeze remediation)", () => {
  afterEach(() => vi.unstubAllGlobals());

  const OPTION_A_V1 = {
    id: "so-a",
    product_id: "p1",
    name: "Option A",
    quantity_units: "1.000000",
    price: "5.00",
    packaging_cost: "0.00",
    sort_order: 0,
    is_active: true,
    version: 1,
  };
  const OPTION_B_V1 = {
    id: "so-b",
    product_id: "p1",
    name: "Option B",
    quantity_units: "2.000000",
    price: "8.00",
    packaging_cost: "0.00",
    sort_order: 1,
    is_active: true,
    version: 1,
  };

  it("submits the version captured when Edit was clicked, not a live prop version that advanced mid-edit (scenario: another row's action refetches the parent Product while this row stays in edit mode)", async () => {
    const OPTION_A_V2 = { ...OPTION_A_V1, version: 2 };
    let getCallCount = 0;
    let archiveBCallCount = 0;
    let patchACallCount = 0;
    let capturedPatchBody: unknown;

    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/auth/me")) {
        return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
      }
      if (method === "GET" && /\/api\/v1\/products\/p1$/.test(url)) {
        getCallCount += 1;
        return Promise.resolve(
          jsonResponse(200, {
            ...PRODUCT_NO_OPTIONS,
            selling_options:
              getCallCount === 1
                ? [OPTION_A_V1, OPTION_B_V1]
                : [OPTION_A_V2, { ...OPTION_B_V1, is_active: false, version: 2 }],
          }),
        );
      }
      if (method === "POST" && /\/api\/v1\/products\/p1\/selling-options\/so-b\/archive$/.test(url)) {
        archiveBCallCount += 1;
        return Promise.resolve(jsonResponse(200, { ...OPTION_B_V1, is_active: false, version: 2 }));
      }
      if (method === "PATCH" && /\/api\/v1\/products\/p1\/selling-options\/so-a$/.test(url)) {
        patchACallCount += 1;
        capturedPatchBody = JSON.parse(init!.body as string);
        // The real server is already at version 2 — a version-1 submission must be rejected.
        return Promise.resolve(
          jsonResponse(409, {
            error: {
              code: "STALE_VERSION",
              message: "This record changed since you opened it.",
              issues: [],
            },
          }),
        );
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    const rowA = screen.getByText("Option A").closest("tr");
    if (!rowA) throw new Error("expected to find Option A's row");
    await act(async () => {
      fireEvent.click(within(rowA).getByRole("button", { name: /^edit$/i }));
    });
    fireEvent.change(within(rowA).getByLabelText(/^name$/i), {
      target: { value: "Option A (edited in this tab)" },
    });

    // A different row's action triggers the parent Product refetch while A remains
    // mid-edit — this is what advances A's `option` prop to version 2 behind the scenes.
    const rowB = screen.getByText("Option B").closest("tr");
    if (!rowB) throw new Error("expected to find Option B's row");
    await act(async () => {
      fireEvent.click(within(rowB).getByRole("button", { name: /^archive$/i }));
    });
    await waitFor(() => expect(archiveBCallCount).toBe(1));
    await waitFor(() => expect(getCallCount).toBe(2));

    // A's row re-rendered with the newer prop (version 2) but the in-progress edit must
    // not have been silently resynced or discarded.
    expect(within(rowA).getByLabelText(/^name$/i)).toHaveValue("Option A (edited in this tab)");

    await act(async () => {
      fireEvent.click(within(rowA).getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(patchACallCount).toBe(1));
    expect(capturedPatchBody).toMatchObject({ version: 1 });
    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();
  });

  it("initializes the edit form and the PATCH version from the current option when it was refetched to a newer version before Edit was ever clicked", async () => {
    const OPTION_A_V2 = { ...OPTION_A_V1, name: "Option A (v2 name)", version: 2 };
    let getCallCount = 0;
    let archiveBCallCount = 0;
    let patchACallCount = 0;
    let capturedPatchBody: unknown;

    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/auth/me")) {
        return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
      }
      if (method === "GET" && /\/api\/v1\/products\/p1$/.test(url)) {
        getCallCount += 1;
        return Promise.resolve(
          jsonResponse(200, {
            ...PRODUCT_NO_OPTIONS,
            selling_options:
              getCallCount === 1
                ? [OPTION_A_V1, OPTION_B_V1]
                : [OPTION_A_V2, { ...OPTION_B_V1, is_active: false, version: 2 }],
          }),
        );
      }
      if (method === "POST" && /\/api\/v1\/products\/p1\/selling-options\/so-b\/archive$/.test(url)) {
        archiveBCallCount += 1;
        return Promise.resolve(jsonResponse(200, { ...OPTION_B_V1, is_active: false, version: 2 }));
      }
      if (method === "PATCH" && /\/api\/v1\/products\/p1\/selling-options\/so-a$/.test(url)) {
        patchACallCount += 1;
        capturedPatchBody = JSON.parse(init!.body as string);
        return Promise.resolve(jsonResponse(200, { ...OPTION_A_V2, version: 3 }));
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    // Refetch Option A to v2 while it sits untouched in view mode (no Edit click yet).
    const rowB = screen.getByText("Option B").closest("tr");
    if (!rowB) throw new Error("expected to find Option B's row");
    await act(async () => {
      fireEvent.click(within(rowB).getByRole("button", { name: /^archive$/i }));
    });
    await waitFor(() => expect(archiveBCallCount).toBe(1));
    await waitFor(() => expect(getCallCount).toBe(2));
    await screen.findByText("Option A (v2 name)");

    const rowA = screen.getByText("Option A (v2 name)").closest("tr");
    if (!rowA) throw new Error("expected to find Option A's row");
    await act(async () => {
      fireEvent.click(within(rowA).getByRole("button", { name: /^edit$/i }));
    });

    expect(within(rowA).getByLabelText(/^name$/i)).toHaveValue("Option A (v2 name)");

    await act(async () => {
      fireEvent.click(within(rowA).getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(patchACallCount).toBe(1));
    expect(capturedPatchBody).toMatchObject({ version: 2 });
  });
});

describe("Product detail — Selling Options", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the empty state and an add-option form for a freshly-created product", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_NO_OPTIONS),
      },
    ]);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    expect(screen.getByText(/no selling options yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add selling option/i })).toBeInTheDocument();
  });

  it("shows a not-found state for a missing/foreign-tenant product", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/nope$/,
        respond: () =>
          jsonResponse(404, {
            error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/products/nope");
    expect(await screen.findByRole("heading", { name: /product not found/i })).toBeInTheDocument();
  });

  it("reflects the API's ordering after a mutation, not the previous fetch's row order (manual-testing remediation)", async () => {
    // Reproduces the reported "stays in creation/mutation order" symptom directly at the
    // rendering layer: the second fetch (triggered by the edit's invalidate/refetch)
    // returns a DIFFERENT order than the first. The component must follow whatever order
    // the latest fetch provides — it must never keep rendering the first fetch's order.
    const ALPHA_OPTION = {
      id: "so-alpha",
      product_id: "p1",
      name: "Alpha Option",
      quantity_units: "1.000000",
      price: "5.00",
      packaging_cost: "0.00",
      sort_order: 10,
      is_active: true,
      version: 1,
    };
    const BETA_OPTION = {
      id: "so-beta",
      product_id: "p1",
      name: "Beta Option",
      quantity_units: "12.000000",
      price: "50.00",
      packaging_cost: "0.00",
      sort_order: 20,
      is_active: true,
      version: 1,
    };
    const ALPHA_MOVED_PAST_BETA = { ...ALPHA_OPTION, sort_order: 25, version: 2 };

    let getCallCount = 0;
    let patchCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(200, {
            ...PRODUCT_NO_OPTIONS,
            selling_options:
              getCallCount === 1
                ? [ALPHA_OPTION, BETA_OPTION]
                : [BETA_OPTION, ALPHA_MOVED_PAST_BETA],
          });
        },
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/products\/p1\/selling-options\/so-alpha$/,
        respond: () => {
          patchCallCount += 1;
          return jsonResponse(200, ALPHA_MOVED_PAST_BETA);
        },
      },
    ]);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });

    const namesInOrder = () =>
      screen.getAllByText(/^(Alpha Option|Beta Option)$/).map((el) => el.textContent);

    await waitFor(() => expect(namesInOrder()).toEqual(["Alpha Option", "Beta Option"]));

    const alphaRow = screen.getByText("Alpha Option").closest("tr");
    if (!alphaRow) throw new Error("expected to find Alpha Option's row");
    await act(async () => {
      fireEvent.click(within(alphaRow).getByRole("button", { name: /^edit$/i }));
    });

    fireEvent.change(within(alphaRow).getByLabelText(/sort order/i), {
      target: { value: "25" },
    });
    await act(async () => {
      fireEvent.click(within(alphaRow).getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(patchCallCount).toBe(1));
    await waitFor(() => expect(getCallCount).toBe(2));

    // The second fetch put Beta first — the rendered row order must follow it, not stay
    // pinned to the first fetch's Alpha-then-Beta order.
    await waitFor(() => expect(namesInOrder()).toEqual(["Beta Option", "Alpha Option"]));
  });
});
