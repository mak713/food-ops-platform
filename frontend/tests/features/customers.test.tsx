// Customer feature tests (Phase 3 plan v3 §17), following the App.test.tsx pattern
// (fetch mocking, full router render via createMemoryRouter).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

const CUSTOMER = {
  id: "c1",
  name: "Ada Lovelace",
  phone: "555-0100",
  email: "ada@example.com",
  preferred_contact_method: null,
  notes: null,
  is_active: true,
  version: 1,
};

describe("Customer list", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the empty state when there are no customers", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 50, offset: 0 }),
      },
    ]);

    renderAt("/app/customers");
    expect(await screen.findByText(/no customers yet/i)).toBeInTheDocument();
  });

  it("renders customers returned by the list endpoint", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "c1",
                name: "Ada Lovelace",
                phone: "555-0100",
                email: "ada@example.com",
                is_active: true,
                version: 1,
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/customers");
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("555-0100")).toBeInTheDocument();
  });

  it("shows an error banner with retry when the list request fails", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () =>
          jsonResponse(500, {
            error: { code: "INTERNAL_SERVER_ERROR", message: "Server error.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/customers");
    expect(await screen.findByRole("alert")).toHaveTextContent("Server error.");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});

describe("Customer create form", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("shows a validation error for a blank name and never calls the API", async () => {
    mockFetchRouter([]);
    renderAt("/app/customers/new");

    await screen.findByRole("heading", { name: /new customer/i });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save/i }));
    });

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
  });

  it("shows the duplicate-warning panel and lets the user create anyway", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "POST",
        pattern: /\/api\/v1\/customers$/,
        respond: (url) => {
          void url;
          createCallCount += 1;
          if (createCallCount === 1) {
            return jsonResponse(422, {
              error: {
                code: "CUSTOMER_CREATE_WARNING",
                message: "A customer that looks like a duplicate already exists.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "POSSIBLE_DUPLICATE_CUSTOMER",
                    message: "A customer with a matching name already exists.",
                    field: "name",
                    resource: "customer",
                    details: { matched_customer_id: "existing-1" },
                  },
                ],
              },
            });
          }
          return jsonResponse(201, CUSTOMER);
        },
      },
    ]);

    renderAt("/app/customers/new");

    await screen.findByRole("heading", { name: /new customer/i });
    fireEvent.change(screen.getByLabelText(/^name$/i), {
      target: { value: "Ada Lovelace" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(
      await screen.findByText(/a customer with a matching name already exists/i),
    ).toBeInTheDocument();
    // Form data preserved after the correctable warning (Spec §12.8).
    expect(screen.getByLabelText(/^name$/i)).toHaveValue("Ada Lovelace");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /create anyway/i }));
    });

    await waitFor(() => expect(createCallCount).toBe(2));
  });
});

describe("Customer edit form — prerequisite-GET failure handling (Checkpoint 3 remediation)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a not-found state and never calls create when the prerequisite GET 404s", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/missing-id$/,
        respond: () =>
          jsonResponse(404, {
            error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
          }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/customers$/,
        respond: () => {
          createCallCount += 1;
          return jsonResponse(201, CUSTOMER);
        },
      },
    ]);

    renderAt("/app/customers/missing-id/edit");

    expect(await screen.findByRole("heading", { name: /customer not found/i })).toBeInTheDocument();
    // The form (and therefore its submit handler) never rendered at all.
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(createCallCount).toBe(0);
  });

  it("shows an error banner with retry when the prerequisite GET fails with a server error, and never calls create", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/broken-id$/,
        respond: () =>
          jsonResponse(500, {
            error: { code: "INTERNAL_SERVER_ERROR", message: "Server error.", issues: [] },
          }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/customers$/,
        respond: () => {
          createCallCount += 1;
          return jsonResponse(201, CUSTOMER);
        },
      },
    ]);

    renderAt("/app/customers/broken-id/edit");

    expect(await screen.findByRole("alert")).toHaveTextContent("Server error.");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(createCallCount).toBe(0);
  });

  it("recovers from a stale-version conflict via Refresh, discarding the stale edit", async () => {
    let patchCallCount = 0;
    let getCallCount = 0;
    const FRESH_CUSTOMER = { ...CUSTOMER, name: "Ada Lovelace (updated elsewhere)", version: 2 };

    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/c1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(200, getCallCount === 1 ? CUSTOMER : FRESH_CUSTOMER);
        },
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/customers\/c1$/,
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

    renderAt("/app/customers/c1/edit");
    await screen.findByRole("heading", { name: /edit customer/i });
    expect(await screen.findByLabelText(/^name$/i)).toHaveValue("Ada Lovelace");

    fireEvent.change(screen.getByLabelText(/^phone$/i), { target: { value: "555-9999" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();
    expect(patchCallCount).toBe(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });

    // Refresh refetched authoritative state and discarded the stale local edit.
    await waitFor(() => expect(screen.getByLabelText(/^name$/i)).toHaveValue(FRESH_CUSTOMER.name));
    expect(screen.queryByText(/changed since you opened it/i)).not.toBeInTheDocument();
    expect(getCallCount).toBe(2);
  });
});

describe("Customer detail", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a not-found state for a missing or foreign-tenant customer, with no history section", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/c1$/,
        respond: () =>
          jsonResponse(200, CUSTOMER),
      },
    ]);

    renderAt("/app/customers/c1");
    await screen.findByRole("heading", { name: "Ada Lovelace" });

    // No Order/history section rendered at all (Phase 3 plan v3 §13).
    expect(screen.queryByText(/order/i)).not.toBeInTheDocument();
  });

  it("renders a 'not found' state for a 404, identical for missing vs foreign-tenant", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/nope$/,
        respond: () =>
          jsonResponse(404, {
            error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/customers/nope");
    expect(await screen.findByRole("heading", { name: /customer not found/i })).toBeInTheDocument();
  });

  it("displays a saved preferred contact method (manual-testing remediation)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/c1$/,
        respond: () =>
          jsonResponse(200, { ...CUSTOMER, preferred_contact_method: "Text message" }),
      },
    ]);

    renderAt("/app/customers/c1");
    await screen.findByRole("heading", { name: "Ada Lovelace" });

    expect(screen.getByText("Preferred contact method")).toBeInTheDocument();
    expect(screen.getByText("Text message")).toBeInTheDocument();
  });

  it("shows a generic connectivity message, not a blank banner, when the server returns a non-JSON error response (manual-testing remediation)", async () => {
    // Reproduces the real failure mode: when the backend is unreachable, Vite's dev
    // proxy answers with its own error response (a non-2xx status, empty/non-JSON body)
    // rather than the fetch call itself throwing. `apiFetch` still wraps that as an
    // ApiError, but with `body: null` — passing `undefined` here reproduces exactly that
    // (see `jsonResponse`'s handling of an undefined body: null body, no Content-Type).
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/c1$/,
        respond: () => jsonResponse(502, undefined),
      },
    ]);

    renderAt("/app/customers/c1");

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/something went wrong/i);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
