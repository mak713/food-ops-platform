// Recipe/RecipeRevision feature tests (Phase 4 Plan v4 §14), following the
// products.test.tsx pattern. Covers: Product Detail recipe summary states, atomic
// creation, the stale `expected_current_revision_id` conflict-and-Refresh regression, the
// archived-carried-forward-ingredient UI treatment, paginated revision history, and the
// all-pages ingredient selector.

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

// Selecting a base-ui Select option in jsdom requires pointerdown+pointerup (it commits
// selection on pointerup, not on a synthetic `click` alone).
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

const PRODUCED_PRODUCT = {
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

const FLOUR = {
  id: "i1",
  name: "Flour",
  measurement_family: "WEIGHT",
  canonical_unit: "g",
  is_active: true,
  version: 1,
};

const WATER = {
  id: "i2",
  name: "Water",
  measurement_family: "VOLUME",
  canonical_unit: "mL",
  is_active: true,
  version: 1,
};

function allActivePage(items: { id: string }[]) {
  return jsonResponse(200, { items, total: items.length, limit: 200, offset: 0 });
}

const REVISION_1 = {
  id: "rev1",
  recipe_id: "recipe1",
  revision_number: 1,
  yield_quantity: "10.000000",
  active_time_minutes: 30,
  elapsed_time_minutes: null,
  notes: null,
  is_current: true,
  ingredients: [
    {
      id: "line1",
      ingredient_id: "i1",
      ingredient_name: "Flour",
      ingredient_is_active: true,
      quantity: "500.000000",
      unit: "g",
    },
  ],
};

const RECIPE = { id: "recipe1", product_id: "p1", name: "Classic Sourdough", current_revision: REVISION_1 };

describe("Product detail — Recipe summary", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a 'no recipe yet' empty state with a Create Recipe link for a PRODUCED product with no recipe", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () =>
            jsonResponse(404, {
              error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
            }),
        },
      ]),
    );

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    expect(await screen.findByText(/no recipe yet/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /create recipe/i })).toHaveAttribute(
      "href",
      "/app/products/p1/recipe/new",
    );
  });

  it("shows the current revision summary with Edit/History links when a recipe exists, with human-readable Yield/quantities and dashes for absent elapsed time/notes", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/recipe$/, respond: () => jsonResponse(200, RECIPE) },
      ]),
    );

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    expect(await screen.findByText("Classic Sourdough")).toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
    // Yield "10.000000" is displayed formatted, never with the raw database-scale padding.
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.queryByText("10.000000")).not.toBeInTheDocument();
    expect(screen.getByText("30 minutes")).toBeInTheDocument();
    // REVISION_1 has null elapsed_time_minutes/notes — a dash, never a literal "null".
    expect(screen.queryByText(/null/i)).not.toBeInTheDocument();
    // Ingredient line: name, formatted quantity, unit.
    expect(screen.getByText("Flour")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
    expect(screen.queryByText("500.000000")).not.toBeInTheDocument();
    expect(screen.getAllByText("g").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /edit recipe/i })).toHaveAttribute(
      "href",
      "/app/products/p1/recipe/edit",
    );
    expect(screen.getByRole("link", { name: /view history/i })).toHaveAttribute(
      "href",
      "/app/products/p1/recipe/history",
    );
  });

  it("shows elapsed time, notes, and an Archived badge on the current recipe when present", async () => {
    const REVISION_WITH_OPTIONAL_FIELDS = {
      ...REVISION_1,
      elapsed_time_minutes: 90,
      notes: "Proof overnight in the fridge.",
      ingredients: [
        ...REVISION_1.ingredients,
        {
          id: "line2",
          ingredient_id: "i-salt",
          ingredient_name: "Salt",
          ingredient_is_active: false,
          quantity: "5.000000",
          unit: "g",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () => jsonResponse(200, { ...RECIPE, current_revision: REVISION_WITH_OPTIONAL_FIELDS }),
        },
      ]),
    );

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    expect(await screen.findByText("90 minutes")).toBeInTheDocument();
    expect(screen.getByText("Proof overnight in the fridge.")).toBeInTheDocument();
    expect(screen.getByText("Salt")).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
  });

  it("does not query for a recipe at all for a PURCHASED product", async () => {
    const PURCHASED = { ...PRODUCED_PRODUCT, product_type: "PURCHASED" };
    const fetchMock = fetchRouterFor([
      { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PURCHASED) },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    renderAt("/app/products/p1");
    await screen.findByRole("heading", { name: "Sourdough Loaf" });
    expect(screen.queryByText(/recipe/i)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/recipe"))).toBe(false);
  });
});

describe("Recipe creation — atomic Recipe + Revision 1", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a Recipe with its ingredient lines in one request and redirects to the product", async () => {
    let capturedBody: unknown;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(201, RECIPE);
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/new");
    await screen.findByRole("heading", { name: /create recipe/i });

    fireEvent.change(screen.getByLabelText(/recipe name/i), { target: { value: "Classic Sourdough" } });
    fireEvent.change(screen.getByLabelText(/^yield$/i), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/active time/i), { target: { value: "30" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add ingredient/i }));
    });
    const line = screen.getByLabelText(/^quantity$/i).closest("div.rounded-md") as HTMLElement;
    fireEvent.change(within(line).getByLabelText(/^quantity$/i), { target: { value: "500" } });
    selectOptionWithin(line, /^unit$/i, /g \(grams\)/);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /create recipe/i }));
    });

    await waitFor(() =>
      expect(capturedBody).toMatchObject({
        name: "Classic Sourdough",
        yield_quantity: "10",
        active_time_minutes: 30,
        ingredients: [{ ingredient_id: "i1", quantity: "500", unit: "g" }],
      }),
    );
    expect(await screen.findByRole("heading", { name: "Sourdough Loaf" })).toBeInTheDocument();
  });

  it("clears a line's unit when its Ingredient is changed to a different measurement family, and blocks submission until a compatible unit is chosen", async () => {
    let postCallCount = 0;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR, WATER]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () => {
            postCallCount += 1;
            return jsonResponse(201, { ...RECIPE, current_revision: { ...RECIPE.current_revision } });
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/new");
    await screen.findByRole("heading", { name: /create recipe/i });

    fireEvent.change(screen.getByLabelText(/recipe name/i), { target: { value: "Sourdough" } });
    fireEvent.change(screen.getByLabelText(/^yield$/i), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/active time/i), { target: { value: "30" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add ingredient/i }));
    });
    const line = screen.getByLabelText(/^quantity$/i).closest("div.rounded-md") as HTMLElement;
    fireEvent.change(within(line).getByLabelText(/^quantity$/i), { target: { value: "500" } });

    // Select the WEIGHT unit "g" for the default (Flour, WEIGHT) line.
    selectOptionWithin(line, /^unit$/i, /g \(grams\)/);
    expect(within(line).getByLabelText(/^unit$/i)).toHaveTextContent("g");

    // Switch this same line's Ingredient to Water (VOLUME) — the stale "g" must not survive.
    selectOptionWithin(line, /^ingredient$/i, "Water");
    expect(within(line).getByLabelText(/^unit$/i)).not.toHaveTextContent("g");

    // Attempting to submit with no unit re-selected is blocked client-side — no POST fires.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /create recipe/i }));
    });
    expect(await screen.findByText(/unit is required/i)).toBeInTheDocument();
    expect(postCallCount).toBe(0);

    // Choosing a valid VOLUME unit allows submission to proceed correctly.
    selectOptionWithin(line, /^unit$/i, /mL \(milliliters\)/);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /create recipe/i }));
    });
    await waitFor(() => expect(postCallCount).toBe(1));
  });

  it("displays the selected Ingredient's human-readable name (never its raw ID) in the closed Select, while still submitting the ID", async () => {
    const UUID_INGREDIENT = {
      id: "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6",
      name: "Vanilla Extract",
      measurement_family: "WEIGHT",
      canonical_unit: "g",
      is_active: true,
      version: 1,
    };
    let capturedBody: { ingredients?: { ingredient_id: string }[] } | undefined;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/ingredients\?/,
          respond: () => allActivePage([FLOUR, UUID_INGREDIENT]),
        },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(201, RECIPE);
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/new");
    await screen.findByRole("heading", { name: /create recipe/i });

    fireEvent.change(screen.getByLabelText(/recipe name/i), { target: { value: "Vanilla Cake" } });
    fireEvent.change(screen.getByLabelText(/^yield$/i), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/active time/i), { target: { value: "30" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add ingredient/i }));
    });
    const line = screen.getByLabelText(/^quantity$/i).closest("div.rounded-md") as HTMLElement;
    fireEvent.change(within(line).getByLabelText(/^quantity$/i), { target: { value: "2" } });

    // The default appended line starts on Flour (activeIngredients[0]) — switch it to the
    // UUID-identified Ingredient.
    selectOptionWithin(line, /^ingredient$/i, "Vanilla Extract");

    const ingredientTrigger = within(line).getByLabelText(/^ingredient$/i);
    expect(ingredientTrigger).toHaveTextContent("Vanilla Extract");
    expect(ingredientTrigger).not.toHaveTextContent(UUID_INGREDIENT.id);

    selectOptionWithin(line, /^unit$/i, /g \(grams\)/);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /create recipe/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody?.ingredients).toEqual([
      { ingredient_id: UUID_INGREDIENT.id, quantity: "2", unit: "g" },
    ]);
  });
});

describe("Recipe edit / create — Cancel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("Recipe Edit Cancel returns to Product Detail without submitting anything or creating a revision", async () => {
    let postCallCount = 0;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/products\/p1\/recipe$/, respond: () => jsonResponse(200, RECIPE) },
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe\/revisions$/,
          respond: () => {
            postCallCount += 1;
            return jsonResponse(201, REVISION_1);
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/edit");
    await screen.findByRole("heading", { name: /edit recipe/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    });

    expect(await screen.findByRole("heading", { name: "Sourdough Loaf" })).toBeInTheDocument();
    expect(postCallCount).toBe(0);
  });

  it("Recipe Create Cancel returns to Product Detail without submitting anything or creating a recipe", async () => {
    let postCallCount = 0;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () => {
            postCallCount += 1;
            return jsonResponse(201, RECIPE);
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/new");
    await screen.findByRole("heading", { name: /create recipe/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    });

    expect(await screen.findByRole("heading", { name: "Sourdough Loaf" })).toBeInTheDocument();
    expect(postCallCount).toBe(0);
  });
});

describe("Recipe edit — preserves a valid elapsed_time_minutes of 0", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("hydrates the elapsed-time input with '0' (not blank) when the current revision has active_time_minutes: 0 and elapsed_time_minutes: 0, and submits elapsed_time_minutes as numeric 0, never null", async () => {
    // Regression: toFormValues() previously used a truthiness check
    // (`revision.elapsed_time_minutes ? String(...) : ""`), which treated a valid `0` the
    // same as "absent" — hydrating the edit form blank and letting an unrelated save
    // silently convert 0 into null.
    const REVISION_WITH_ZERO_TIMES = {
      ...REVISION_1,
      active_time_minutes: 0,
      elapsed_time_minutes: 0,
    };
    const RECIPE_WITH_ZERO_TIMES = { ...RECIPE, current_revision: REVISION_WITH_ZERO_TIMES };
    let capturedBody: { elapsed_time_minutes?: number | null } | undefined;

    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () => jsonResponse(200, RECIPE_WITH_ZERO_TIMES),
        },
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe\/revisions$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(201, { ...REVISION_WITH_ZERO_TIMES, id: "rev2", revision_number: 2 });
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/edit");
    await screen.findByRole("heading", { name: /edit recipe/i });

    // Displays "0" rather than a blank input for a genuinely zero elapsed time.
    expect(await screen.findByLabelText(/elapsed time/i)).toHaveValue("0");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save as new revision/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody?.elapsed_time_minutes).toBe(0);
    expect(capturedBody?.elapsed_time_minutes).not.toBeNull();
  });
});

describe("Recipe edit — stale expected_current_revision_id regression", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("submits the revision id captured at edit-session start, surfaces RECIPE_REVISION_CONFLICT distinctly, and re-captures the new current revision id after Refresh", async () => {
    // Regression coverage for the concurrency design (Plan v4 §6a/§13): the edit session
    // captures `expected_current_revision_id` once, at load — a rejected submission must
    // surface the conflict distinctly, and Refresh must re-capture the *new* current
    // revision id before allowing another save to succeed.
    let createRevisionCallCount = 0;
    let getRecipeCallCount = 0;
    const capturedExpectedIds: string[] = [];
    const REVISION_2 = { ...REVISION_1, id: "rev2", revision_number: 2 };
    const RECIPE_AT_REV2 = { ...RECIPE, current_revision: REVISION_2 };

    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () => {
            getRecipeCallCount += 1;
            return jsonResponse(200, getRecipeCallCount === 1 ? RECIPE : RECIPE_AT_REV2);
          },
        },
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe\/revisions$/,
          respond: (_url, init) => {
            createRevisionCallCount += 1;
            const body = JSON.parse(init!.body as string);
            capturedExpectedIds.push(body.expected_current_revision_id);
            if (createRevisionCallCount === 1) {
              return jsonResponse(409, {
                error: {
                  code: "RECIPE_REVISION_CONFLICT",
                  message: "This recipe changed since you started editing.",
                  issues: [],
                },
              });
            }
            return jsonResponse(201, REVISION_2);
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/edit");
    await screen.findByRole("heading", { name: /edit recipe/i });
    expect(await screen.findByLabelText(/^yield$/i)).toHaveValue("10.000000");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save as new revision/i }));
    });

    expect(await screen.findByText(/changed since you started editing/i)).toBeInTheDocument();
    expect(createRevisionCallCount).toBe(1);
    // The first submission carried the revision id captured at session start — rev1.
    expect(capturedExpectedIds[0]).toBe("rev1");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });
    await waitFor(() => expect(getRecipeCallCount).toBe(2));
    expect(screen.queryByText(/changed since you started editing/i)).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save as new revision/i }));
    });

    await waitFor(() => expect(createRevisionCallCount).toBe(2));
    // After Refresh, the session re-captured the new current revision id — rev2 — and the
    // second submission carries that, not the stale rev1.
    expect(capturedExpectedIds[1]).toBe("rev2");
    expect(await screen.findByRole("heading", { name: "Sourdough Loaf" })).toBeInTheDocument();
  });
});

describe("Recipe edit — archived carried-forward ingredient", () => {
  afterEach(() => vi.unstubAllGlobals());

  const REVISION_WITH_ARCHIVED = {
    ...REVISION_1,
    ingredients: [
      ...REVISION_1.ingredients,
      {
        id: "line2",
        ingredient_id: "i-salt",
        ingredient_name: "Salt",
        ingredient_is_active: false,
        quantity: "5.000000",
        unit: "g",
      },
    ],
  };
  const RECIPE_WITH_ARCHIVED = { ...RECIPE, current_revision: REVISION_WITH_ARCHIVED };

  it("shows a carried-forward archived ingredient's line as read-only/labeled, not offered again in the Add-ingredient selector, and preserves it unedited on save", async () => {
    let capturedBody: unknown;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe$/,
          respond: () => jsonResponse(200, RECIPE_WITH_ARCHIVED),
        },
        // The active-ingredient selector never lists the archived Salt — only Flour.
        { method: "GET", pattern: /\/api\/v1\/ingredients\?/, respond: () => allActivePage([FLOUR]) },
        {
          method: "POST",
          pattern: /\/api\/v1\/products\/p1\/recipe\/revisions$/,
          respond: (_url, init) => {
            capturedBody = JSON.parse(init!.body as string);
            return jsonResponse(201, { ...REVISION_WITH_ARCHIVED, id: "rev2", revision_number: 2 });
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/edit");
    await screen.findByRole("heading", { name: /edit recipe/i });
    await screen.findByText("Salt");

    const saltLine = screen.getByText("Salt").closest("div.rounded-md") as HTMLElement;
    expect(within(saltLine).getByText(/archived/i)).toBeInTheDocument();
    // No Select trigger for the ingredient on the archived line — it's fixed, not editable.
    expect(within(saltLine).queryByLabelText(/^ingredient$/i)).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save as new revision/i }));
    });

    await waitFor(() => expect(capturedBody).toBeDefined());
    expect(capturedBody).toMatchObject({
      ingredients: expect.arrayContaining([
        { ingredient_id: "i-salt", quantity: "5.000000", unit: "g" },
      ]),
    });
  });
});

describe("Recipe revision history — pagination", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reaches a revision beyond the first page via the Next control", async () => {
    const page1Items = Array.from({ length: 20 }, (_, i) => ({
      id: `rev-${20 - i}`,
      revision_number: 21 - i,
      yield_quantity: "10.000000",
      is_current: 21 - i === 21,
    }));
    const page2Items = [
      { id: "rev-1", revision_number: 1, yield_quantity: "9.000000", is_current: false },
    ];

    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe\/revisions\?/,
          respond: (url) => {
            const offset = Number(new URL(url, "http://x").searchParams.get("offset") ?? "0");
            return jsonResponse(200, {
              items: offset === 0 ? page1Items : page2Items,
              total: 21,
              limit: 20,
              offset,
            });
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/history");
    await screen.findByRole("heading", { name: /recipe revision history/i });
    expect(await screen.findByText("#20")).toBeInTheDocument();
    expect(screen.queryByText("#1")).not.toBeInTheDocument();
    // Yield "10.000000" is displayed formatted ("10"), never with raw database padding.
    expect(screen.getAllByText("10").length).toBeGreaterThan(0);
    expect(screen.queryByText("10.000000")).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^next$/i }));
    });

    expect(await screen.findByText("#1")).toBeInTheDocument();
    expect(screen.getByText(/showing 21–21 of 21/i)).toBeInTheDocument();
    // Page 2's "9.000000" is likewise formatted down to "9".
    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.queryByText("9.000000")).not.toBeInTheDocument();
  });
});

describe("Recipe revision detail — human-readable Decimal formatting", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("formats Yield and ingredient-line quantities for humans, without losing precision on a six-decimal value", async () => {
    const DETAIL_REVISION = {
      id: "rev1",
      recipe_id: "recipe1",
      revision_number: 1,
      yield_quantity: "12.500000",
      active_time_minutes: 20,
      elapsed_time_minutes: null,
      notes: null,
      is_current: true,
      ingredients: [
        {
          id: "line1",
          ingredient_id: "i1",
          ingredient_name: "Flour",
          ingredient_is_active: true,
          quantity: "12.000000",
          unit: "g",
        },
        {
          id: "line2",
          ingredient_id: "i2",
          ingredient_name: "Yeast",
          ingredient_is_active: true,
          quantity: "0.000001",
          unit: "g",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        {
          method: "GET",
          pattern: /\/api\/v1\/products\/p1\/recipe\/revisions\/rev1$/,
          respond: () => jsonResponse(200, DETAIL_REVISION),
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/revisions/rev1");
    await screen.findByRole("heading", { name: /revision #1/i });

    // Trailing-zero decimal: "12.500000" -> "12.5".
    expect(screen.getByText("12.5")).toBeInTheDocument();
    expect(screen.queryByText("12.500000")).not.toBeInTheDocument();
    // Integer-looking: "12.000000" -> "12".
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.queryByText("12.000000")).not.toBeInTheDocument();
    // Six-decimal precision is preserved exactly, never rounded away or truncated.
    expect(screen.getByText("0.000001")).toBeInTheDocument();
  });
});

describe("Recipe editor — all-pages ingredient selector", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("offers an ingredient that only appears on the second page of the active-ingredients list", async () => {
    const page1 = Array.from({ length: 200 }, (_, i) => ({
      id: `i-${i}`,
      name: `Ingredient ${i}`,
      measurement_family: "WEIGHT",
      canonical_unit: "g",
      is_active: true,
      version: 1,
    }));
    const lastIngredient = {
      id: "i-200",
      name: "Vanilla Extract",
      measurement_family: "WEIGHT",
      canonical_unit: "g",
      is_active: true,
      version: 1,
    };

    let ingredientsCallCount = 0;
    vi.stubGlobal(
      "fetch",
      fetchRouterFor([
        { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCED_PRODUCT) },
        {
          method: "GET",
          pattern: /\/api\/v1\/ingredients\?/,
          respond: (url) => {
            ingredientsCallCount += 1;
            const offset = Number(new URL(url, "http://x").searchParams.get("offset") ?? "0");
            if (offset === 0) {
              return jsonResponse(200, { items: page1, total: 201, limit: 200, offset: 0 });
            }
            return jsonResponse(200, { items: [lastIngredient], total: 201, limit: 200, offset: 200 });
          },
        },
      ]),
    );

    renderAt("/app/products/p1/recipe/new");
    await screen.findByRole("heading", { name: /create recipe/i });
    await waitFor(() => expect(ingredientsCallCount).toBe(2));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add ingredient/i }));
    });
    const line = screen.getByLabelText(/^quantity$/i).closest("div.rounded-md") as HTMLElement;
    fireEvent.click(within(line).getByLabelText(/^ingredient$/i));
    expect(screen.getByText("Vanilla Extract")).toBeInTheDocument();
  });
});
