// Ingredient feature tests (Phase 4 Plan v4 §14), following the App.test.tsx /
// products.test.tsx pattern.

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

// Selecting a base-ui Select option in jsdom requires pointerdown+pointerup (it commits
// selection on pointerup, not on a synthetic `click` alone).
function selectOption(triggerLabel: RegExp | string, optionText: RegExp | string) {
  fireEvent.click(screen.getByLabelText(triggerLabel));
  const option = screen.getByText(optionText);
  fireEvent.pointerDown(option, { pointerId: 1, button: 0 });
  fireEvent.pointerUp(option, { pointerId: 1, button: 0 });
  fireEvent.click(option);
}

const FLOUR = {
  id: "i1",
  name: "Flour",
  measurement_family: "WEIGHT",
  canonical_unit: "g",
  is_active: true,
  version: 1,
};

describe("Ingredient list", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the empty state when there are no ingredients", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 50, offset: 0 }),
      },
    ]);

    renderAt("/app/inventory/ingredients");
    expect(await screen.findByText(/no ingredients yet/i)).toBeInTheDocument();
  });

  it("renders ingredients returned by the list endpoint", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\?/,
        respond: () => jsonResponse(200, { items: [FLOUR], total: 1, limit: 50, offset: 0 }),
      },
    ]);

    renderAt("/app/inventory/ingredients");
    expect(await screen.findByText("Flour")).toBeInTheDocument();
    expect(screen.getByText("WEIGHT")).toBeInTheDocument();
    expect(screen.getByText("g")).toBeInTheDocument();
  });
});

describe("Ingredient create form", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a validation error for a blank name and never calls the API", async () => {
    mockFetchRouter([]);
    renderAt("/app/inventory/ingredients/new");

    await screen.findByRole("heading", { name: /new ingredient/i });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
  });

  it("creates an Ingredient with the chosen measurement family/canonical unit and redirects to its detail page", async () => {
    let capturedBody: unknown;
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/auth/me")) return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
      if (method === "POST" && url.endsWith("/api/v1/ingredients")) {
        capturedBody = JSON.parse(init!.body as string);
        return Promise.resolve(jsonResponse(201, FLOUR));
      }
      if (method === "GET" && /\/api\/v1\/ingredients\/i1$/.test(url)) {
        return Promise.resolve(jsonResponse(200, FLOUR));
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/inventory/ingredients/new");
    await screen.findByRole("heading", { name: /new ingredient/i });
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "Flour" } });
    // Family and canonical unit default to WEIGHT / g — left at their defaults.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByRole("heading", { name: "Flour" })).toBeInTheDocument();
    expect(capturedBody).toMatchObject({
      name: "Flour",
      measurement_family: "WEIGHT",
      canonical_unit: "g",
    });
  });

  it("offers only units belonging to the selected measurement family", async () => {
    mockFetchRouter([]);
    renderAt("/app/inventory/ingredients/new");
    await screen.findByRole("heading", { name: /new ingredient/i });

    selectOption(/measurement family/i, "VOLUME");
    fireEvent.click(screen.getByLabelText(/canonical unit/i));
    expect(screen.getByText(/mL \(milliliters\)/)).toBeInTheDocument();
    expect(screen.queryByText(/g \(grams\)/)).not.toBeInTheDocument();
  });

  it("resets canonical_unit away from the previous family's unit when measurement_family changes, so switching WEIGHT to VOLUME cannot submit canonical_unit='g'", async () => {
    let capturedBody: { canonical_unit?: string; measurement_family?: string } | undefined;
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/auth/me")) return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
      if (method === "POST" && url.endsWith("/api/v1/ingredients")) {
        capturedBody = JSON.parse(init!.body as string);
        return Promise.resolve(jsonResponse(201, { ...FLOUR, measurement_family: "VOLUME", canonical_unit: "mL" }));
      }
      if (method === "GET" && /\/api\/v1\/ingredients\/i1$/.test(url)) {
        return Promise.resolve(jsonResponse(200, FLOUR));
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/inventory/ingredients/new");
    await screen.findByRole("heading", { name: /new ingredient/i });
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "Milk" } });

    // The form starts at its WEIGHT/"g" default (matching the create test above) —
    // switch to VOLUME without ever touching canonical_unit directly.
    selectOption(/measurement family/i, "VOLUME");

    // The canonical unit Select must no longer show the stale WEIGHT "g" value.
    expect(screen.getByLabelText(/canonical unit/i)).not.toHaveTextContent("g");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody?.measurement_family).toBe("VOLUME");
    expect(capturedBody?.canonical_unit).not.toBe("g");
    expect(capturedBody?.canonical_unit).toBe("mL");
  });
});

describe("Ingredient edit form", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows measurement family/canonical unit as immutable text and only allows editing the name", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\/i1$/,
        respond: () => jsonResponse(200, FLOUR),
      },
    ]);

    renderAt("/app/inventory/ingredients/i1/edit");
    await screen.findByRole("heading", { name: /edit ingredient/i });
    expect(screen.getByLabelText(/^name$/i)).toHaveValue("Flour");
    expect(screen.getByText(/cannot be changed after creation/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/measurement family/i)).not.toBeInTheDocument();
  });

  it("recovers from a stale-version conflict via Refresh", async () => {
    let patchCallCount = 0;
    let getCallCount = 0;
    const FRESH = { ...FLOUR, name: "Renamed elsewhere", version: 2 };

    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\/i1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(200, getCallCount === 1 ? FLOUR : FRESH);
        },
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/ingredients\/i1$/,
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

    renderAt("/app/inventory/ingredients/i1/edit");
    await screen.findByRole("heading", { name: /edit ingredient/i });
    expect(await screen.findByLabelText(/^name$/i)).toHaveValue("Flour");

    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "Locally renamed" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();
    expect(patchCallCount).toBe(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });

    await waitFor(() => expect(screen.getByLabelText(/^name$/i)).toHaveValue(FRESH.name));
    expect(screen.queryByText(/changed since you opened it/i)).not.toBeInTheDocument();
  });
});

describe("Ingredient detail — lifecycle actions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("archives an active ingredient", async () => {
    let archiveCallCount = 0;
    let getCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\/i1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(200, getCallCount === 1 ? FLOUR : { ...FLOUR, is_active: false, version: 2 });
        },
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/ingredients\/i1\/archive$/,
        respond: () => {
          archiveCallCount += 1;
          return jsonResponse(200, { ...FLOUR, is_active: false, version: 2 });
        },
      },
    ]);

    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^archive$/i }));
    });
    const dialog = await screen.findByRole("alertdialog");
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /^archive$/i }));
    });

    await waitFor(() => expect(archiveCallCount).toBe(1));
    expect(await screen.findByText("Archived")).toBeInTheDocument();
  });

  it("closes the confirmation dialog after a blocked delete, keeping the error visible and the ingredient intact", async () => {
    let deleteCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\/i1$/,
        respond: () => jsonResponse(200, FLOUR),
      },
      {
        method: "DELETE",
        pattern: /\/api\/v1\/ingredients\/i1\?/,
        respond: () => {
          deleteCallCount += 1;
          return jsonResponse(409, {
            error: {
              code: "INGREDIENT_HAS_REFERENCES",
              message: "This ingredient is used elsewhere and cannot be deleted. Archive it instead.",
              issues: [],
            },
          });
        },
      },
    ]);

    renderAt("/app/inventory/ingredients/i1");
    await screen.findByRole("heading", { name: "Flour" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    });
    const dialog = await screen.findByRole("alertdialog");
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    });

    await waitFor(() => expect(deleteCallCount).toBe(1));
    // The dialog must no longer trap the user in the confirmation state.
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    // The blocking error must be clearly visible now that the overlay is gone.
    expect(await screen.findByText(/used elsewhere and cannot be deleted/i)).toBeInTheDocument();
    // The ingredient itself must remain present, and Archive must still be offered.
    expect(screen.getByRole("heading", { name: "Flour" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^archive$/i })).toBeInTheDocument();
  });

  it("shows a not-found state for a missing/foreign-tenant ingredient", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/ingredients\/nope$/,
        respond: () =>
          jsonResponse(404, {
            error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/inventory/ingredients/nope");
    expect(await screen.findByRole("heading", { name: /ingredient not found/i })).toBeInTheDocument();
  });
});
