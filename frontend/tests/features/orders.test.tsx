// Order feature tests (Phase 6 Final Plan §J), following customers.test.tsx's exact
// template (mockFetchRouter, createMemoryRouter, real QueryClientProvider/AuthProvider).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";

// Explicit cleanup, not relying solely on RTL's auto-registered afterEach — a base-ui
// Select popup is portaled with `keepMounted: true` for its close transition, and in
// JSDOM (which never fires the transitionend/animationend event that would normally
// trigger the deferred physical removal) that portal's content can outlive `cleanup()`
// itself, orphaned outside React's own tracked tree — confirmed directly: even after
// `cleanup()`, a stale "Sourdough Loaf" option from "Standard Option real workflow"
// remained in `document.body` and collided with a later, unrelated test that renders
// its own identically-labeled option. Clearing `document.body` directly guarantees no
// DOM survives between tests regardless of which library left it behind.
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

const ORDER_LINE = {
  id: "line-1",
  line_type: "STANDARD_OPTION",
  product_id: "p1",
  selling_option_id: "so1",
  display_name_snapshot: "Sourdough Loaf — 6-pack",
  package_quantity: "2.000000",
  underlying_quantity: "12.000000",
  charged_unit_price_snapshot: "12.00",
  line_subtotal: "24.00",
  packaging_cost_per_package_snapshot: "0.00",
  packaging_cost_total_snapshot: "0.00",
  price_override_reason: null,
  custom_direct_cost_estimate: null,
  custom_active_time_minutes: null,
  manual_fulfillment_required: false,
  manual_fulfillment_satisfied: false,
  notes: null,
};

const ORDER = {
  id: "o1",
  order_number: "ORD-ABCDEF012345",
  status: "DRAFT",
  customer_id: null,
  fulfillment_date: null,
  fulfillment_time: null,
  fulfillment_method: null,
  fulfillment_details: null,
  fulfillment_notes: null,
  internal_notes: null,
  subtotal: "24.00",
  order_adjustment: "0.00",
  adjustment_description: null,
  manual_tax: "0.00",
  final_total: "24.00",
  estimated_direct_cost: null,
  estimated_contribution: null,
  estimated_contribution_margin: null,
  version: 1,
  lines: [ORDER_LINE],
  payments: [],
  payments_total: "0.00",
  payment_status: "UNPAID",
  overpayment_amount: null,
  is_confirmable: false,
  confirmation_issues: [
    { severity: "ERROR", code: "ORDER_MISSING_FULFILLMENT_DATE", message: "A fulfillment date is required before confirmation.", field: "fulfillment_date" },
  ],
};

describe("Order list", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the empty state when there are no orders", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 50, offset: 0 }),
      },
    ]);

    renderAt("/app/orders");
    expect(await screen.findByText(/no orders yet/i)).toBeInTheDocument();
  });

  it("renders orders returned by the list endpoint", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "o1",
                order_number: "ORD-ABCDEF012345",
                status: "DRAFT",
                customer_id: null,
                customer_name: null,
                fulfillment_date: null,
                final_total: "24.00",
                payment_status: "UNPAID",
                version: 1,
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/orders");
    expect(await screen.findByText("ORD-ABCDEF012345")).toBeInTheDocument();
    expect(screen.getByText("Guest")).toBeInTheDocument();
  });

  it("shows an error banner with retry when the list request fails", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\?/,
        respond: () =>
          jsonResponse(500, {
            error: { code: "INTERNAL_SERVER_ERROR", message: "Server error.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/orders");
    expect(await screen.findByRole("alert")).toHaveTextContent("Server error.");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});

describe("Order Entry", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("has no Confirm button anywhere on the entry page", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });
    expect(screen.queryByRole("button", { name: /confirm/i })).not.toBeInTheDocument();
  });

  it("CUSTOM_QUANTITY line has no package-quantity input", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom quantity line/i }));
    });

    expect(screen.queryByLabelText(/package quantity/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^quantity$/i)).toBeInTheDocument();
  });

  it("CUSTOM_ITEM line defaults its manual-fulfillment toggle to checked", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom item line/i }));
    });

    const checkbox = screen.getByRole("checkbox", { name: /requires manual fulfillment/i });
    expect(checkbox).toBeChecked();
  });

  it("adds an initial-Payment overpayment warning with an acknowledge action", async () => {
    let createCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders$/,
        respond: () => {
          createCallCount += 1;
          if (createCallCount === 1) {
            return jsonResponse(422, {
              error: {
                code: "PAYMENT_OVERAGE_WARNING",
                message: "This would result in the order being overpaid.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "PAYMENT_WOULD_OVERPAY",
                    message: "Recording this would exceed the order's total.",
                    field: "amount",
                    resource: "payment",
                    details: {},
                  },
                ],
              },
            });
          }
          return jsonResponse(201, ORDER);
        },
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: /record a payment now/i }));
    });
    fireEvent.change(screen.getByLabelText(/^amount$/i), { target: { value: "50.00" } });
    fireEvent.change(screen.getByLabelText(/payment method/i), { target: { value: "cash" } });
    fireEvent.change(screen.getByLabelText(/payment date/i), { target: { value: "2026-01-01" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(
      await screen.findByText(/recording this would exceed the order's total/i),
    ).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /confirm anyway/i }));
    });

    await waitFor(() => expect(createCallCount).toBe(2));
  });
});

const PRODUCT_WITH_OPTION = {
  id: "p1",
  name: "Sourdough Loaf",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [
    {
      id: "so1",
      product_id: "p1",
      name: "6-pack",
      quantity_units: "6.000000",
      price: "12.00",
      packaging_cost: "0.00",
      sort_order: 0,
      is_active: true,
      version: 1,
    },
  ],
};

async function openSelectAndPick(triggerName: RegExp, optionName: RegExp) {
  // The Selling Option trigger stays disabled until the Product detail query resolves
  // (populating its options) — wait for the trigger to actually be interactive before
  // clicking, or the click is a silent no-op and the popup never opens.
  await waitFor(() => expect(screen.getByRole("combobox", { name: triggerName })).toBeEnabled());
  await act(async () => {
    fireEvent.click(screen.getByRole("combobox", { name: triggerName }));
  });
  await act(async () => {
    const option = await screen.findByRole("option", { name: optionName });
    // base-ui's SelectItem ignores a bare `click` unless the item is already
    // highlighted (which only happens by luck for a single-item list) or a real
    // `pointerdown` preceded it (which sets its internal allowMouseSelectionRef) —
    // matching real browser event order avoids relying on incidental highlight state.
    fireEvent.pointerDown(option, { pointerType: "mouse" });
    fireEvent.click(option);
  });
}

describe("Order Entry — Standard Option real workflow (correction 1 regression)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a Standard Option order via the real seller workflow without requiring underlying_quantity", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p1",
                name: "Sourdough Loaf",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(201, ORDER);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, ORDER),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    await openSelectAndPick(/^product$/i, /sourdough loaf/i);
    await openSelectAndPick(/selling option/i, /6-pack/i);

    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "2" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[])[0];
    expect(line.line_type).toBe("STANDARD_OPTION");
    expect(line.product_id).toBe("p1");
    expect(line.selling_option_id).toBe("so1");
    expect(line.package_quantity).toBe("2");
    // The regression this test guards against: a STANDARD_OPTION line has no
    // underlying_quantity input and must not be blocked by (or required to send) one.
    expect(line.underlying_quantity == null || line.underlying_quantity === "").toBe(true);

    expect(
      await screen.findByRole("heading", { name: "ORD-ABCDEF012345" }),
    ).toBeInTheDocument();
  });
});

describe("Order Entry — Guest/Walk-In switching (correction 6)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("switches from a selected Customer back to Guest / Walk-In", async () => {
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
                phone: null,
                email: null,
                is_active: true,
                version: 1,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(
      /guest \/ walk-in/i,
    );

    await openSelectAndPick(/^customer$/i, /^ada lovelace$/i);
    expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(
      "Ada Lovelace",
    );

    await openSelectAndPick(/^customer$/i, /guest \/ walk-in/i);
    expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(
      /guest \/ walk-in/i,
    );
  });
});

describe("Order Entry — Custom Item optional fields (correction 4)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("includes optional direct-cost-estimate and active-time-minutes in the submitted payload", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(201, ORDER);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, ORDER),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom item line/i }));
    });
    fireEvent.change(screen.getByLabelText(/^description$/i), {
      target: { value: "Custom cake topper" },
    });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText(/unit price/i), { target: { value: "10.00" } });
    fireEvent.change(screen.getByLabelText(/internal direct-cost estimate/i), {
      target: { value: "3.00" },
    });
    fireEvent.change(screen.getByLabelText(/estimated active time/i), {
      target: { value: "15" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[])[0];
    expect(line.custom_direct_cost_estimate).toBe("3.00");
    expect(line.custom_active_time_minutes).toBe(15);
  });
});

const EDIT_ORDER = {
  ...ORDER,
  lines: [
    ORDER_LINE,
    {
      id: "line-2",
      line_type: "CUSTOM_QUANTITY",
      product_id: "p2",
      selling_option_id: null,
      display_name_snapshot: "Chocolate Chip Cookie",
      package_quantity: "1.000000",
      underlying_quantity: "30.000000",
      charged_unit_price_snapshot: "55.00",
      line_subtotal: "55.00",
      packaging_cost_per_package_snapshot: "0.10",
      packaging_cost_total_snapshot: "0.10",
      price_override_reason: null,
      custom_direct_cost_estimate: null,
      custom_active_time_minutes: null,
      manual_fulfillment_required: false,
      manual_fulfillment_satisfied: false,
      notes: null,
    },
  ],
};

describe("Order Entry — snapshot edit-intent (corrections 2/3)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("leaves Standard Option price-override and Custom Quantity packaging-override fields blank on edit, and omits them from an unrelated-edit save", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, EDIT_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p1",
                name: "Sourdough Loaf",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(200, EDIT_ORDER);
        },
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    const priceOverrideInput = await screen.findByLabelText(/price override/i);
    expect(priceOverrideInput).toHaveValue("");
    const packagingOverrideInput = screen.getByLabelText(/packaging cost override/i);
    expect(packagingOverrideInput).toHaveValue("");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const lines = capturedBody!.lines as Record<string, unknown>[];
    const standardOptionLine = lines.find((l) => l.line_type === "STANDARD_OPTION")!;
    const customQuantityLine = lines.find((l) => l.line_type === "CUSTOM_QUANTITY")!;
    expect(
      standardOptionLine.charged_unit_price == null || standardOptionLine.charged_unit_price === "",
    ).toBe(true);
    expect(
      customQuantityLine.packaging_cost_per_package == null ||
        customQuantityLine.packaging_cost_per_package === "",
    ).toBe(true);
  });
});

describe("Order Entry — stable server OrderLine identity", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("resubmits the retained line's real server id, sends no id for a new line, and doesn't corrupt the untouched line's id", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, EDIT_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(200, EDIT_ORDER);
        },
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    // Remove the CUSTOM_QUANTITY line (line-2), add a new CUSTOM_ITEM line, leave the
    // STANDARD_OPTION line (line-1) untouched.
    const removeButtons = screen.getAllByRole("button", { name: /^remove$/i });
    await act(async () => {
      fireEvent.click(removeButtons[1]);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom item line/i }));
    });
    fireEvent.change(screen.getByLabelText(/^description$/i), { target: { value: "Extra" } });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText(/unit price/i), { target: { value: "5.00" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const lines = capturedBody!.lines as Record<string, unknown>[];
    expect(lines).toHaveLength(2);
    const retained = lines.find((l) => l.line_type === "STANDARD_OPTION")!;
    const added = lines.find((l) => l.line_type === "CUSTOM_ITEM")!;
    expect(retained.id).toBe("line-1");
    expect(added.id == null).toBe(true);
    expect(lines.some((l) => l.id === "line-2")).toBe(false);
  });
});

const PRODUCT_P2 = {
  id: "p2",
  name: "Chocolate Chip Cookie",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.10",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [],
};

describe("Order Entry — controlled OrderLine edits mark the form dirty (Final Hardening §1 BLOCKER)", () => {
  afterEach(() => vi.unstubAllGlobals());

  const PRODUCT_WITH_TWO_ACTIVE_OPTIONS = {
    ...PRODUCT_WITH_OPTION,
    selling_options: [
      PRODUCT_WITH_OPTION.selling_options[0],
      {
        id: "so2",
        product_id: "p1",
        name: "12-pack",
        quantity_units: "12.000000",
        price: "22.00",
        packaging_cost: "0.00",
        sort_order: 1,
        is_active: true,
        version: 1,
      },
    ],
  };

  function mockEditRoute() {
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true }],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_TWO_ACTIVE_OPTIONS),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
    ]);
  }

  it("changing an existing Standard Option line's Selling Option through the controlled selector triggers the unsaved-changes warning on navigation", async () => {
    mockEditRoute();
    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await openSelectAndPick(/selling option/i, /12-pack/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("link", { name: /^orders$/i }));
    });

    expect(
      await screen.findByRole("heading", { name: /discard unsaved changes/i }),
    ).toBeInTheDocument();
  });

  it("changing an existing line's type marks the form dirty", async () => {
    mockEditRoute();
    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await openSelectAndPick(/^line 1 type$/i, /custom quantity/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("link", { name: /^orders$/i }));
    });

    expect(
      await screen.findByRole("heading", { name: /discard unsaved changes/i }),
    ).toBeInTheDocument();
  });
});

const INACTIVE_CUSTOMER = {
  id: "c-arch",
  name: "Grace Hopper",
  phone: null,
  email: null,
  preferred_contact_method: null,
  notes: null,
  is_active: false,
  version: 1,
};

const EDIT_ORDER_WITH_INACTIVE_CUSTOMER = { ...EDIT_ORDER, customer_id: "c-arch" };

describe("Order Entry — carried-forward inactive references (Final Hardening §2 BLOCKER)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a carried-forward inactive Customer as itself (not Guest) and preserves it through an unrelated edit", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, EDIT_ORDER_WITH_INACTIVE_CUSTOMER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/c-arch$/,
        respond: () => jsonResponse(200, INACTIVE_CUSTOMER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true }],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCT_WITH_OPTION) },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(200, EDIT_ORDER_WITH_INACTIVE_CUSTOMER);
        },
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    const trigger = screen.getByRole("combobox", { name: /^customer$/i });
    await waitFor(() => expect(trigger).toHaveTextContent(/grace hopper/i));
    expect(trigger).toHaveTextContent(/archived/i);
    expect(trigger).not.toHaveTextContent(/guest/i);

    fireEvent.change(screen.getByLabelText(/internal notes/i), {
      target: { value: "unrelated edit" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    expect(capturedBody!.customer_id).toBe("c-arch");
  });

  it("removes an inactive Customer from future selection choices once the seller switches away from it", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, EDIT_ORDER_WITH_INACTIVE_CUSTOMER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\/c-arch$/,
        respond: () => jsonResponse(200, INACTIVE_CUSTOMER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCT_WITH_OPTION) },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(/grace hopper/i),
    );

    await openSelectAndPick(/^customer$/i, /guest \/ walk-in/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /^customer$/i }));
    });
    expect(screen.queryByRole("option", { name: /grace hopper/i })).not.toBeInTheDocument();
  });

  it("keeps a retained archived Standard Option Selling Option visible and preserves it through an unrelated edit", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    const PRODUCT_WITH_MIXED_OPTIONS = {
      ...PRODUCT_WITH_OPTION,
      selling_options: [
        { ...PRODUCT_WITH_OPTION.selling_options[0], is_active: false },
        {
          id: "so2",
          product_id: "p1",
          name: "12-pack",
          quantity_units: "12.000000",
          price: "22.00",
          packaging_cost: "0.00",
          sort_order: 1,
          is_active: true,
          version: 1,
        },
      ],
    };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true }],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_MIXED_OPTIONS),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(200, EDIT_ORDER);
        },
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    const sellingOptionTrigger = await screen.findByRole("combobox", { name: /selling option/i });
    await waitFor(() => expect(sellingOptionTrigger).toHaveTextContent(/6-pack/i));
    expect(sellingOptionTrigger).toHaveTextContent(/archived/i);

    fireEvent.change(screen.getByLabelText(/internal notes/i), {
      target: { value: "unrelated edit" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "STANDARD_OPTION",
    )!;
    expect(line.product_id).toBe("p1");
    expect(line.selling_option_id).toBe("so1");
  });

  it("never offers an archived Selling Option as a new choice", async () => {
    const PRODUCT_WITH_MIXED_OPTIONS = {
      ...PRODUCT_WITH_OPTION,
      selling_options: [
        { ...PRODUCT_WITH_OPTION.selling_options[0], is_active: false },
        {
          id: "so2",
          product_id: "p1",
          name: "12-pack",
          quantity_units: "12.000000",
          price: "22.00",
          packaging_cost: "0.00",
          sort_order: 1,
          is_active: true,
          version: 1,
        },
      ],
    };
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true }],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_MIXED_OPTIONS),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await openSelectAndPick(/^product$/i, /sourdough loaf/i);

    const sellingOptionTrigger = await screen.findByRole("combobox", { name: /selling option/i });
    await waitFor(() => expect(sellingOptionTrigger).toBeEnabled());
    await act(async () => {
      fireEvent.click(sellingOptionTrigger);
    });
    expect(await screen.findByRole("option", { name: /12-pack/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /6-pack/i })).not.toBeInTheDocument();
  });

  it("disables the Selling Option selector when the current Product itself is inactive", async () => {
    const INACTIVE_PRODUCT_WITH_OPTION = {
      ...PRODUCT_WITH_OPTION,
      is_active: false,
      selling_options: [{ ...PRODUCT_WITH_OPTION.selling_options[0], is_active: false }],
    };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, INACTIVE_PRODUCT_WITH_OPTION),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    const sellingOptionTrigger = await screen.findByRole("combobox", { name: /selling option/i });
    await waitFor(() => expect(sellingOptionTrigger).toBeDisabled());
  });

  it("keeps a retained archived Custom Quantity Product visible and preserves it through an unrelated edit", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    const INACTIVE_PRODUCT_P2 = { ...PRODUCT_P2, is_active: false };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [{ id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true }],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCT_WITH_OPTION) },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p2$/,
        respond: () => jsonResponse(200, INACTIVE_PRODUCT_P2),
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(200, EDIT_ORDER);
        },
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    const customQuantityProductTrigger = screen.getByRole("combobox", { name: /line 2 product/i });
    await waitFor(() => expect(customQuantityProductTrigger).toHaveTextContent(/chocolate chip cookie/i));
    expect(customQuantityProductTrigger).toHaveTextContent(/archived/i);

    fireEvent.change(screen.getByLabelText(/internal notes/i), {
      target: { value: "unrelated edit" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "CUSTOM_QUANTITY",
    )!;
    expect(line.product_id).toBe("p2");
  });
});

describe("Order Entry — source-change request payload intent (Final Hardening §4)", () => {
  afterEach(() => vi.unstubAllGlobals());

  const PRODUCT_WITH_TWO_ACTIVE_OPTIONS = {
    ...PRODUCT_WITH_OPTION,
    selling_options: [
      PRODUCT_WITH_OPTION.selling_options[0],
      {
        id: "so2",
        product_id: "p1",
        name: "12-pack",
        quantity_units: "12.000000",
        price: "22.00",
        packaging_cost: "0.00",
        sort_order: 1,
        is_active: true,
        version: 1,
      },
    ],
  };

  function mockEditRoute(
    onPatch: (body: Record<string, unknown>) => void,
    orderFixture: typeof EDIT_ORDER = EDIT_ORDER,
  ) {
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, orderFixture) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              { id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
              { id: "p3", name: "Butter Cookie", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
            ],
            total: 2,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_TWO_ACTIVE_OPTIONS),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p3$/,
        respond: () => jsonResponse(200, { ...PRODUCT_P2, id: "p3", name: "Butter Cookie" }),
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          const body = JSON.parse((init?.body as string) ?? "{}");
          onPatch(body);
          return jsonResponse(200, EDIT_ORDER);
        },
      },
    ]);
  }

  it("Standard Option: changing to a different Selling Option and leaving the price override blank sends the new source with no override", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockEditRoute((body) => {
      capturedBody = body;
    });

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await openSelectAndPick(/selling option/i, /12-pack/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "STANDARD_OPTION",
    )!;
    expect(line.product_id).toBe("p1");
    expect(line.selling_option_id).toBe("so2");
    expect(line.charged_unit_price == null || line.charged_unit_price === "").toBe(true);
  });

  it("Standard Option: entering an explicit price override after a source change sends that override", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockEditRoute((body) => {
      capturedBody = body;
    });

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await openSelectAndPick(/selling option/i, /12-pack/i);
    fireEvent.change(screen.getByLabelText(/price override/i), { target: { value: "20.00" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "STANDARD_OPTION",
    )!;
    expect(line.selling_option_id).toBe("so2");
    expect(line.charged_unit_price).toBe("20.00");
  });

  it("Custom Quantity: changing to a different Product and leaving the packaging override blank sends the new Product with no override", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockEditRoute((body) => {
      capturedBody = body;
    });

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await openSelectAndPick(/line 2 product/i, /butter cookie/i);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "CUSTOM_QUANTITY",
    )!;
    expect(line.product_id).toBe("p3");
    expect(
      line.packaging_cost_per_package == null || line.packaging_cost_per_package === "",
    ).toBe(true);
  });

  it("Custom Quantity: entering an explicit packaging override after a Product change sends that override", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockEditRoute((body) => {
      capturedBody = body;
    });

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await openSelectAndPick(/line 2 product/i, /butter cookie/i);
    fireEvent.change(screen.getByLabelText(/packaging cost override/i), { target: { value: "0.25" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "CUSTOM_QUANTITY",
    )!;
    expect(line.product_id).toBe("p3");
    expect(line.packaging_cost_per_package).toBe("0.25");
  });

  it("clears a stale price-override reason when the Selling Option source changes without a new explicit override", async () => {
    const EDIT_ORDER_WITH_REASON = {
      ...EDIT_ORDER,
      lines: [
        { ...ORDER_LINE, price_override_reason: "Bulk discount for repeat customer" },
        EDIT_ORDER.lines[1],
      ],
    };
    let capturedBody: Record<string, unknown> | null = null;
    mockEditRoute((body) => {
      capturedBody = body;
    }, EDIT_ORDER_WITH_REASON);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    expect(await screen.findByLabelText(/override reason/i)).toHaveValue(
      "Bulk discount for repeat customer",
    );

    await openSelectAndPick(/selling option/i, /12-pack/i);

    expect(screen.getByLabelText(/override reason/i)).toHaveValue("");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[]).find(
      (l) => l.line_type === "STANDARD_OPTION",
    )!;
    expect(line.price_override_reason == null || line.price_override_reason === "").toBe(true);
  });
});

describe("Order Edit — stale-version recovery", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the stale-version panel and Refresh resets the form to the fresh server state", async () => {
    let getCallCount = 0;
    const FRESH_ORDER = { ...ORDER, internal_notes: "updated elsewhere", version: 2 };
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => {
          getCallCount += 1;
          return jsonResponse(200, getCallCount === 1 ? ORDER : FRESH_ORDER);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () =>
          jsonResponse(409, {
            error: {
              code: "STALE_VERSION",
              message:
                "This record changed since you opened it. Refresh the latest version and review your changes before saving again.",
              issues: [],
            },
          }),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });
    expect(await screen.findByLabelText(/internal notes/i)).toHaveValue("");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/changed since you opened it/i)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    });

    await waitFor(() =>
      expect(screen.getByLabelText(/internal notes/i)).toHaveValue("updated elsewhere"),
    );
    expect(screen.queryByText(/changed since you opened it/i)).not.toBeInTheDocument();
    expect(getCallCount).toBe(2);
  });
});

describe("Order Entry — confirmed-edit operational warning review (Finding 8)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the warning review and 'Save anyway' resubmits with the acknowledged fingerprint", async () => {
    let patchCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "PATCH",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: (_url, init) => {
          patchCallCount += 1;
          const body = JSON.parse(init?.body as string);
          if ((body.acknowledged_warning_fingerprints ?? []).length === 0) {
            return jsonResponse(422, {
              error: {
                code: "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT",
                message: "This order has operational warnings that must be reviewed before saving.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "INGREDIENT_SHORTAGE",
                    message: "There is not enough of this ingredient to cover demand.",
                    field: null,
                    resource: "ingredient",
                    details: { fingerprint: "INGREDIENT_SHORTAGE:ing1:5.000000" },
                  },
                ],
              },
            });
          }
          return jsonResponse(200, { ...CONFIRMED_ORDER, version: CONFIRMED_ORDER.version + 1 });
        },
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(
      await screen.findByText(/not enough of this ingredient to cover demand/i),
    ).toBeInTheDocument();
    const saveAnyway = screen.getByRole("button", { name: /save anyway/i });

    await act(async () => {
      fireEvent.click(saveAnyway);
    });

    await waitFor(() => expect(patchCallCount).toBe(2));
  });
});

describe("Order Entry — Operational Impact Preview panel (Finding 8)", () => {
  afterEach(() => vi.unstubAllGlobals());

  const PREVIEW_RESPONSE = {
    subtotal: "60.00",
    final_total: "60.00",
    warnings: [
      {
        severity: "WARNING",
        code: "INGREDIENT_SHORTAGE",
        message: "There is not enough of this ingredient to cover demand.",
        field: null,
        resource: "ingredient",
        details: { fingerprint: "INGREDIENT_SHORTAGE:ing1:20.000000" },
      },
    ],
    warning_fingerprints: ["INGREDIENT_SHORTAGE:ing1:20.000000"],
    production_requirements: [
      {
        product_id: "p1",
        recipe_revision_id: "rev1",
        demand_date: "2026-02-01",
        is_protected: false,
        missing_recipe: false,
        baseline_confirmed_demand_quantity: "0.000000",
        baseline_surplus_allocated_quantity: "0.000000",
        baseline_production_demand_quantity: "0.000000",
        baseline_recommended_batches: null,
        projected_confirmed_demand_quantity: "6.000000",
        projected_surplus_allocated_quantity: "0.000000",
        projected_production_demand_quantity: "6.000000",
        projected_recommended_batches: 1,
        projected_expected_output_quantity: "12.000000",
        projected_expected_excess_quantity: "6.000000",
        projected_estimated_active_minutes: 30,
        projected_estimated_elapsed_minutes: 30,
        projected_estimated_ingredient_cost: "5.00",
        projected_estimated_labor_cost: "7.50",
        projected_estimated_direct_production_cost: "12.50",
        projected_suggested_start_at: null,
        incremental_confirmed_demand_quantity: "6.000000",
        incremental_production_demand_quantity: "6.000000",
      },
    ],
    ingredient_availability: [
      {
        ingredient_id: "ing1",
        ingredient_name: "Test Flour",
        canonical_unit: "g",
        physical_quantity: "10.000000",
        baseline_shortage_quantity: "0.000000",
        projected_shortage_quantity: "20.000000",
      },
    ],
    purchased_shortages: [],
    custom_item_workload: [],
  };

  it("renders the server-computed baseline/projected production requirements and warnings for a new Order", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, { items: [PRODUCT_WITH_OPTION], total: 1, limit: 200, offset: 0 }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/preview$/,
        respond: () => jsonResponse(200, PREVIEW_RESPONSE),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom quantity line/i }));
    });
    await openSelectAndPick(/line 1 product/i, /sourdough loaf/i);
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText(/agreed line price/i), { target: { value: "60.00" } });

    expect(
      await screen.findByText(/sourdough loaf.*2026-02-01/i, {}, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/not enough of this ingredient to cover demand/i),
    ).toBeInTheDocument();
    // Manual Acceptance UX Correction Plan, Finding 1/3: the Ingredient
    // availability row identifies the Ingredient by name/unit and trims raw
    // storage precision — never "stock 10.000000" with no identity.
    expect(screen.getByText(/test flour \(g\): stock 10 —/i)).toBeInTheDocument();
    expect(screen.queryByText(/10\.000000/i)).not.toBeInTheDocument();
  });

  it("renders the confirmed-order substitution preview (baseline excludes this Order's own contribution)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/preview$/,
        respond: () =>
          jsonResponse(200, {
            ...PREVIEW_RESPONSE,
            production_requirements: [
              {
                ...PREVIEW_RESPONSE.production_requirements[0],
                baseline_confirmed_demand_quantity: "4.000000", // sibling Order's own demand
                projected_confirmed_demand_quantity: "16.000000", // 4 + this Order's 12
                incremental_confirmed_demand_quantity: "12.000000", // never 16
              },
            ],
          }),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    // Manual Acceptance UX Correction Plan, Finding 3: whole-number OIP
    // quantities render trimmed of storage precision, never "4.000000 demand".
    expect(await screen.findByText(/4 demand/i, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByText(/16 demand/i)).toBeInTheDocument();
    expect(screen.getByText(/\+12 demand/i)).toBeInTheDocument();
    expect(screen.queryByText(/4\.000000 demand/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/16\.000000 demand/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/12\.000000 demand/i)).not.toBeInTheDocument();
  });

  it("shows the missing-fulfillment-date advisory instead of fabricating dated operational impact, while the financial preview stays available (Phase 7 Final Semantic & Precision Correction Plan, Finding 5)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, { items: [PRODUCT_WITH_OPTION], total: 1, limit: 200, offset: 0 }),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/preview$/,
        respond: () =>
          jsonResponse(200, {
            ...PREVIEW_RESPONSE,
            warnings: [],
            warning_fingerprints: [],
            production_requirements: [],
            ingredient_availability: [],
            fulfillment_date_required_for_operational_preview: true,
          }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    // Deliberately never touches the "Fulfillment date" field.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom quantity line/i }));
    });
    await openSelectAndPick(/line 1 product/i, /sourdough loaf/i);
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText(/agreed line price/i), { target: { value: "60.00" } });

    expect(
      await screen.findByText(/add a fulfillment date to see operational impact/i, {}, {
        timeout: 3000,
      }),
    ).toBeInTheDocument();

    // No fabricated dated production/workload result is rendered.
    expect(screen.queryByText(/sourdough loaf.*2026-\d\d-\d\d/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/no production impact yet — add a standard option/i),
    ).not.toBeInTheDocument();

    // Financial subtotal/final total remain available (computed client-side from
    // the line values, independent of the backend preview's operational fields).
    expect(getPreviewTotalText()).toBe("$60.00");
  });

  it("shows Custom Item manual workload without the contradictory 'no production impact yet' empty state (Manual Acceptance UX Correction Plan, Finding 4)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/preview$/,
        respond: () =>
          jsonResponse(200, {
            ...PREVIEW_RESPONSE,
            warnings: [],
            warning_fingerprints: [],
            production_requirements: [],
            ingredient_availability: [],
            purchased_shortages: [],
            custom_item_workload: [
              {
                demand_date: "2026-02-01",
                baseline_total_active_minutes: 0,
                projected_total_active_minutes: 45,
                baseline_contributing_line_count: 0,
                projected_contributing_line_count: 1,
              },
            ],
          }),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    expect(
      await screen.findByText(/manual\/custom workload/i, {}, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no production impact yet/i)).not.toBeInTheDocument();
  });

  it("shows Purchased stock shortage without the contradictory 'no production impact yet' empty state (Manual Acceptance UX Correction Plan, Finding 4)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/preview$/,
        respond: () =>
          jsonResponse(200, {
            ...PREVIEW_RESPONSE,
            warnings: [],
            warning_fingerprints: [],
            production_requirements: [],
            ingredient_availability: [],
            purchased_shortages: [
              {
                product_id: "p1",
                baseline_shortage_quantity: "0.000000",
                projected_shortage_quantity: "5.000000",
              },
            ],
            custom_item_workload: [],
          }),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    expect(
      await screen.findByText(/purchased stock shortage/i, {}, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no production impact yet/i)).not.toBeInTheDocument();
  });

  it("still shows the empty-state message when the Preview genuinely has no operational result at all", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/preview$/,
        respond: () =>
          jsonResponse(200, {
            ...PREVIEW_RESPONSE,
            warnings: [],
            warning_fingerprints: [],
            production_requirements: [],
            ingredient_availability: [],
            purchased_shortages: [],
            custom_item_workload: [],
          }),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    expect(
      await screen.findByText(/no production impact yet/i, {}, { timeout: 3000 }),
    ).toBeInTheDocument();
  });
});

describe("Order Entry — production-locked field-level granularity (Finding 8, depends on Finding 3)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("disables only demand-affecting controls on a production-locked line, leaving price/notes editable", async () => {
    const LOCKED_ORDER = { ...CONFIRMED_ORDER, production_locked: true };
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, LOCKED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    // Demand-affecting controls: disabled.
    expect(screen.getByRole("combobox", { name: "Line 1 type" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Product" })).toBeDisabled());
    expect(screen.getByRole("combobox", { name: "Selling option" })).toBeDisabled();
    expect(screen.getByLabelText(/package quantity/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled();

    // Non-production fields: still editable.
    const priceOverride = screen.getByLabelText(/price override/i);
    expect(priceOverride).toBeEnabled();
    fireEvent.change(priceOverride, { target: { value: "15.00" } });
    expect(priceOverride).toHaveValue("15.00");

    const notes = screen.getByLabelText(/line notes/i);
    expect(notes).toBeEnabled();
    fireEvent.change(notes, { target: { value: "seller note" } });
    expect(notes).toHaveValue("seller note");

    const reason = screen.getByLabelText(/override reason/i);
    expect(reason).toBeEnabled();

    // The fulfillment date/time fields are also locked (both genuinely
    // operational together).
    expect(screen.getByLabelText(/fulfillment date/i)).toBeDisabled();
    expect(screen.getByLabelText(/fulfillment time/i)).toBeDisabled();

    // Add-line buttons stay conservatively disabled as a whole.
    expect(
      screen.getByRole("button", { name: /add standard option line/i }),
    ).toBeDisabled();
  });
});

describe("Order Entry — unsaved-change navigation warning (correction 5)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("blocks in-app navigation away from a dirty form, and lets the seller discard or stay", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    fireEvent.change(screen.getByLabelText(/internal notes/i), {
      target: { value: "a draft thought" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("link", { name: /^orders$/i }));
    });

    expect(
      await screen.findByRole("heading", { name: /discard unsaved changes/i }),
    ).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /stay on this page/i }));
    });
    expect(screen.getByRole("heading", { name: /new order/i })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("link", { name: /^orders$/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /discard changes/i }));
    });

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: /new order/i })).not.toBeInTheDocument(),
    );
  });

  it("does not block navigation away from an untouched form", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("link", { name: /^orders$/i }));
    });

    expect(
      screen.queryByRole("heading", { name: /discard unsaved changes/i }),
    ).not.toBeInTheDocument();
  });
});

describe("Order Detail", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a not-found state for a missing or foreign-tenant order", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/nope$/,
        respond: () =>
          jsonResponse(404, {
            error: { code: "NOT_FOUND", message: "The requested resource was not found.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/orders/nope");
    expect(await screen.findByRole("heading", { name: /order not found/i })).toBeInTheDocument();
  });

  it("renders lines, payment status, and a disabled Confirm button when not structurally ready", async () => {
    // Phase 7: the Confirm action now genuinely exists (Plan v2 §16) — this test is
    // the documented, approved replacement for the old "no Confirm button" assertion
    // (Plan v2 §19). It now proves the real, correct behavior for a structurally
    // incomplete Draft: the button is present but disabled, never silently hidden.
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, ORDER),
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    expect(screen.getByText("Sourdough Loaf — 6-pack")).toBeInTheDocument();
    expect(screen.getByText("Unpaid")).toBeInTheDocument();
    expect(
      screen.getByText(/a fulfillment date is required before confirmation/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /confirm order/i }),
    ).toBeDisabled();
  });

  it("renders line quantities trimmed of storage precision, not raw six-decimal strings (Manual Acceptance UX Correction Plan, Amendment 2)", async () => {
    const customQuantityLine = {
      ...ORDER_LINE,
      id: "line-2",
      line_type: "CUSTOM_QUANTITY",
      selling_option_id: null,
      display_name_snapshot: "Custom bulk flour",
      package_quantity: "1.000000",
      underlying_quantity: "1.250000",
    };
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, { ...ORDER, lines: [ORDER_LINE, customQuantityLine] }),
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    // STANDARD_OPTION line: "2.000000" package_quantity must render as "2 pkg",
    // never the raw "2.000000 pkg" the seller manually observed.
    expect(screen.getByText("2 pkg")).toBeInTheDocument();
    expect(screen.queryByText("2.000000 pkg")).not.toBeInTheDocument();

    // CUSTOM_QUANTITY line: underlying_quantity renders trimmed, with meaningful
    // fractional precision preserved.
    expect(screen.getByText("1.25")).toBeInTheDocument();
    expect(screen.queryByText("1.250000")).not.toBeInTheDocument();
  });

  it("shows the Draft-delete-with-payments warning and lets the seller delete anyway", async () => {
    let deleteCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, { ...ORDER, payments: [{ id: "pay1", amount: "10.00", payment_method: "cash", payment_date: "2026-01-01", notes: null }] }),
      },
      {
        method: "DELETE",
        pattern: /\/api\/v1\/orders\/o1\?/,
        respond: (url) => {
          deleteCallCount += 1;
          if (!url.includes("confirm_delete_with_payments=true")) {
            return jsonResponse(422, {
              error: {
                code: "ORDER_DELETE_HAS_PAYMENTS_WARNING",
                message: "This draft has recorded payments.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "ORDER_HAS_PAYMENTS",
                    message: "Deleting this draft will also delete its recorded payments.",
                    field: null,
                    resource: "order",
                    details: { payment_count: 1, payment_total: "10.00" },
                  },
                ],
              },
            });
          }
          return jsonResponse(204, undefined);
        },
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    });

    expect(
      await screen.findByText(/deleting this draft will also delete its recorded payments/i),
    ).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /delete anyway/i }));
    });

    await waitFor(() => expect(deleteCallCount).toBe(2));
  });
});

const CONFIRMED_ORDER = {
  ...ORDER,
  status: "CONFIRMED",
  fulfillment_date: "2026-02-01",
  fulfillment_time: "09:00:00",
  is_confirmable: false,
  confirmation_issues: [],
  production_locked: false,
};

describe("Order Detail — Phase 7 lifecycle actions (Finding 8)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("confirms a structurally-ready Draft order with no warnings", async () => {
    let confirmCallCount = 0;
    let confirmed = false;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () =>
          jsonResponse(
            200,
            confirmed
              ? CONFIRMED_ORDER
              : {
                  ...ORDER,
                  fulfillment_date: "2026-02-01",
                  is_confirmable: true,
                  confirmation_issues: [],
                },
          ),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/confirm$/,
        respond: () => {
          confirmCallCount += 1;
          confirmed = true;
          return jsonResponse(200, CONFIRMED_ORDER);
        },
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    const confirmButton = await screen.findByRole("button", { name: "Confirm Order" });
    expect(confirmButton).toBeEnabled();
    await act(async () => {
      fireEvent.click(confirmButton);
    });

    await waitFor(() => expect(confirmCallCount).toBe(1));
    expect(await screen.findByRole("heading", { name: "Confirmed" })).toBeInTheDocument();
  });

  it("shows the operational warning review and resubmits with the acknowledged fingerprint", async () => {
    let confirmCallCount = 0;
    let confirmed = false;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () =>
          jsonResponse(
            200,
            confirmed
              ? CONFIRMED_ORDER
              : {
                  ...ORDER,
                  fulfillment_date: "2026-02-01",
                  is_confirmable: true,
                  confirmation_issues: [],
                },
          ),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/confirm$/,
        respond: (_url, init) => {
          confirmCallCount += 1;
          const body = JSON.parse(init?.body as string);
          if (body.acknowledged_warning_fingerprints.length === 0) {
            return jsonResponse(422, {
              error: {
                code: "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT",
                message: "This order has operational warnings that must be reviewed.",
                issues: [
                  {
                    severity: "WARNING",
                    code: "INGREDIENT_SHORTAGE",
                    message: "There is not enough of this ingredient to cover demand.",
                    field: null,
                    resource: "ingredient",
                    details: { fingerprint: "INGREDIENT_SHORTAGE:ing1:20.000000" },
                  },
                ],
              },
            });
          }
          confirmed = true;
          return jsonResponse(200, CONFIRMED_ORDER);
        },
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Confirm Order" }));
    });

    expect(
      await screen.findByText(/not enough of this ingredient to cover demand/i),
    ).toBeInTheDocument();
    const confirmAnyway = screen.getByRole("button", { name: /confirm anyway/i });

    await act(async () => {
      fireEvent.click(confirmAnyway);
    });

    await waitFor(() => expect(confirmCallCount).toBe(2));
    expect(await screen.findByRole("heading", { name: "Confirmed" })).toBeInTheDocument();
  });

  it("cancels a Confirmed order via the confirmation dialog", async () => {
    let cancelCallCount = 0;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/cancel$/,
        respond: () => {
          cancelCallCount += 1;
          return jsonResponse(200, { ...CONFIRMED_ORDER, status: "CANCELED" });
        },
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel Order" }));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Cancel order" }));
    });

    await waitFor(() => expect(cancelCallCount).toBe(1));
  });

  it("shows an Edit link for a Confirmed order that navigates to the edit page", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, CONFIRMED_ORDER),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_OPTION),
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    await act(async () => {
      fireEvent.click(screen.getByRole("link", { name: "Edit" }));
    });

    await screen.findByRole("heading", { name: /edit order/i });
  });
});

describe("Order Detail — CANCELED order Payment UX boundary (Final Hardening §5)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows historical payments and status but no Add Payment form on a canceled order", async () => {
    const CANCELED_ORDER = {
      ...ORDER,
      status: "CANCELED",
      payments: [
        { id: "pay1", amount: "10.00", payment_method: "cash", payment_date: "2026-01-01", notes: null },
      ],
      payments_total: "10.00",
      payment_status: "PARTIALLY_PAID",
    };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, CANCELED_ORDER) },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    expect(screen.getByText("$10.00")).toBeInTheDocument();
    expect(screen.getByText("Partially Paid")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^add payment$/i })).not.toBeInTheDocument();
    expect(
      screen.getByText(/new payments cannot be recorded on a canceled order/i),
    ).toBeInTheDocument();
  });
});

describe("Order Detail — generic mutation failures are surfaced (Final Hardening §6)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows an error message when Add Payment fails for a reason other than overpayment", async () => {
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, ORDER) },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders\/o1\/payments$/,
        respond: () =>
          jsonResponse(409, {
            error: {
              code: "ORDER_CANCELED_NO_NEW_PAYMENTS",
              message: "This order has been canceled.",
              issues: [],
            },
          }),
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    fireEvent.change(screen.getByLabelText(/^amount$/i), { target: { value: "10.00" } });
    fireEvent.change(screen.getByLabelText(/payment method/i), { target: { value: "cash" } });
    fireEvent.change(screen.getByLabelText(/payment date/i), { target: { value: "2026-01-01" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^add payment$/i }));
    });

    expect(await screen.findByText(/this order has been canceled/i)).toBeInTheDocument();
  });

  it("shows an error message when deleting a draft fails for a reason other than the payments/stale-version warnings", async () => {
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, ORDER) },
      {
        method: "DELETE",
        pattern: /\/api\/v1\/orders\/o1\?/,
        respond: () =>
          jsonResponse(500, {
            error: { code: "INTERNAL_SERVER_ERROR", message: "Something broke on the server.", issues: [] },
          }),
      },
    ]);

    renderAt("/app/orders/o1");
    await screen.findByRole("heading", { name: "ORD-ABCDEF012345" });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    });

    expect(await screen.findByText(/something broke on the server/i)).toBeInTheDocument();
  });
});

// Manual Acceptance Select Fix — real-browser testing found the Product Select trapping
// interaction after a selection: the trigger visually stayed open, outside clicks didn't
// dismiss it, and the sibling Selling Option Select never became usable. Root cause,
// confirmed both by direct DOM inspection in a real Chrome session and by the React
// console warning below: `ProductSellingOptionPicker`'s Product Select and Selling Option
// Select are React siblings under the same parent `<div>`, and both independently used the
// same `key={value || "none"}` fallback pattern — so a brand-new Standard Option line (both
// values unset) gave them the *identical* key `"none"`, a genuine React duplicate-key
// error. React's reconciliation is undefined once only one of two identically-keyed
// siblings later changes key (as happens the instant a Product is picked and its key stops
// being "none"), and in practice this manifested as the Product's own selection failing to
// commit and the Selling Option Select never receiving its Product id.
//
// Select Fix Final Micro-Hardening §1: the first fix (distinct empty-value fallback
// strings, `"product-none"` / `"selling-option-none"`) only guaranteed uniqueness for the
// *unset* case — a Product ID and a Selling Option ID are values from separate tables, so
// sibling React identity still incidentally depended on those UUIDs never colliding for a
// *populated* pair. The keys are now unconditionally namespaced
// (`` `product-${productId ?? "none"}` `` / `` `selling-option-${sellingOptionId ?? "none"}` ``)
// so the two Selects' keys are disjoint by construction for every value, not merely the
// empty one — see `PRODUCT_WITH_COLLIDING_IDS` below, which deliberately gives a Product
// and one of its own Selling Options the identical underlying id string, proving role
// namespacing (not UUID improbability) is what guarantees uniqueness. The value-derived-
// `key`-forces-remount *strategy* itself was not the defect and remains unchanged.
const PRODUCT_B_WITH_OPTIONS = {
  id: "p2",
  name: "Chocolate Chip Cookie",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [
    {
      id: "so2",
      product_id: "p2",
      name: "Dozen",
      quantity_units: "12.000000",
      price: "18.00",
      packaging_cost: "0.00",
      sort_order: 0,
      is_active: true,
      version: 1,
    },
  ],
};

function mockTwoProductCatalog() {
  mockFetchRouter([
    {
      method: "GET",
      pattern: /\/api\/v1\/customers\?/,
      respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
    },
    {
      method: "GET",
      pattern: /\/api\/v1\/products\?/,
      respond: () =>
        jsonResponse(200, {
          items: [
            { id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
            { id: "p2", name: "Chocolate Chip Cookie", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
          ],
          total: 2,
          limit: 200,
          offset: 0,
        }),
    },
    { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCT_WITH_OPTION) },
    { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_B_WITH_OPTIONS) },
  ]);
}

function findDuplicateKeyWarnings(errorSpy: ReturnType<typeof vi.spyOn>) {
  return errorSpy.mock.calls.filter((args) =>
    args.some(
      (a) => typeof a === "string" && a.includes("Encountered two children with the same key"),
    ),
  );
}

// A Product whose own id and one of its Selling Option ids are the identical literal
// string "collide-1" — deliberately, to prove the fix is role namespacing and not merely
// "two random UUIDs are unlikely to match" (Select Fix Final Micro-Hardening §1). If the
// Product Select's key and the Selling Option Select's key were still derived from the
// bare value alone, selecting this Product and then this Selling Option would reproduce
// the exact original duplicate-key defect even though every id here is realistic in
// shape — the only thing preventing it is the `product-`/`selling-option-` prefix.
const PRODUCT_WITH_COLLIDING_IDS = {
  id: "collide-1",
  name: "Collision Test Product",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [
    {
      id: "collide-1",
      product_id: "collide-1",
      name: "Colliding Option",
      quantity_units: "1.000000",
      price: "5.00",
      packaging_cost: "0.00",
      sort_order: 0,
      is_active: true,
      version: 1,
    },
  ],
};

describe("Order Entry — Select popup lifecycle regression (Manual Acceptance Select Fix)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("never emits a duplicate-key warning for a brand-new Standard Option line (both Product and Selling Option unset)", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockTwoProductCatalog();

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("never emits a duplicate-key warning when the selected Product id and Selling Option id are identical strings (proves role namespacing, not UUID improbability, guarantees uniqueness)", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "collide-1",
                name: "Collision Test Product",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/collide-1$/,
        respond: () => jsonResponse(200, PRODUCT_WITH_COLLIDING_IDS),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    // Both the Product's own id and its Selling Option's id are the literal string
    // "collide-1" — once both are selected, the two Selects' *values* are identical,
    // so only the `product-`/`selling-option-` role prefix on each key keeps them from
    // colliding.
    await openSelectAndPick(/^product$/i, /collision test product/i);
    await openSelectAndPick(/selling option/i, /colliding option/i);

    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveTextContent(
      /collision test product/i,
    );
    expect(screen.getByRole("combobox", { name: /selling option/i })).toHaveTextContent(
      /colliding option/i,
    );
    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("faithfully models the real interaction: Product commits and closes, form regains normal interaction, Selling Option opens/selects/closes", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockTwoProductCatalog();

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    // 1-3: open Product Select, choose a Product with the proper pointer sequence
    // (pointerdown then click — a bare click alone is silently ignored by base-ui unless
    // the item happens to already be highlighted), assert the selected Product changed.
    await openSelectAndPick(/^product$/i, /sourdough loaf/i);
    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveTextContent(
      /sourdough loaf/i,
    );

    // 4: assert the Product popup is closed/removed — no role="option"/role="listbox"
    // remains in the document, and aria-expanded on the trigger is false. (JSDOM has no
    // real focus/pointer-capture model, so this is the strongest assertion available here
    // for "the popup is no longer interactive"; the real-browser regression itself was
    // reproduced and verified fixed directly in a live Chrome session, documented in
    // MANUAL_FIX_REPORT.md, since JSDOM cannot exercise actual OS-level pointer capture.)
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    // 5-6: click another ordinary form field; assert the interaction succeeds (the field
    // actually receives the typed value) — this is exactly what stayed broken in the
    // real-browser defect (the Product popup trapped all further interaction).
    fireEvent.change(screen.getByLabelText(/fulfillment-facing notes/i), {
      target: { value: "form still responds" },
    });
    expect(screen.getByLabelText(/fulfillment-facing notes/i)).toHaveValue(
      "form still responds",
    );

    // 7-9: open Selling Option Select, select an option, assert its popup also closes.
    await openSelectAndPick(/selling option/i, /6-pack/i);
    expect(screen.getByRole("combobox", { name: /selling option/i })).toHaveTextContent(
      /6-pack/i,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    // 10: continue filling the form.
    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "2" } });
    expect(screen.getByLabelText(/package quantity/i)).toHaveValue("2");

    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("handles repeated Product source changes (A→B→A) with no duplicate popup, no stale item list, and no trapped interaction", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              { id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
              { id: "p2", name: "Chocolate Chip Cookie", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
            ],
            total: 2,
            limit: 200,
            offset: 0,
          }),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCT_WITH_OPTION) },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_B_WITH_OPTIONS) },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(201, ORDER);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, ORDER),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    // Product A ("Sourdough Loaf") -> Selling Option A ("6-pack").
    await openSelectAndPick(/^product$/i, /sourdough loaf/i);
    await openSelectAndPick(/selling option/i, /6-pack/i);
    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveTextContent(
      /sourdough loaf/i,
    );

    // Product A -> Product B ("Chocolate Chip Cookie") — the prior Selling Option must
    // reset, since "6-pack" belongs to a different Product.
    await openSelectAndPick(/^product$/i, /chocolate chip cookie/i);
    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveTextContent(
      /chocolate chip cookie/i,
    );
    // Never the stale "6-pack" carried over from Product A — immediately after the
    // switch this is "Loading options…" (STANDARD_OPTION End-to-End Fix §7's explicit
    // loading state, while Product B's own detail query is still in flight) and it must
    // settle to the neutral "Select an option" once that query resolves; either way, it
    // must never keep showing Product A's already-selected option.
    const sellingOptionTrigger = screen.getByRole("combobox", { name: /selling option/i });
    expect(sellingOptionTrigger).not.toHaveTextContent(/6-pack/i);
    await waitFor(() => expect(sellingOptionTrigger).toHaveTextContent(/select an option/i));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);

    // Product B -> back to Product A (round trip) — reopening must show exactly the
    // catalog's two products, no duplicates, no leftover Product-B-only items.
    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /^product$/i }));
    });
    const reopenedOptions = await screen.findAllByRole("option");
    expect(reopenedOptions.map((o) => o.textContent)).toEqual([
      "Sourdough Loaf",
      "Chocolate Chip Cookie",
    ]);
    await act(async () => {
      fireEvent.pointerDown(
        screen.getByRole("option", { name: /sourdough loaf/i }),
        { pointerType: "mouse" },
      );
      fireEvent.click(screen.getByRole("option", { name: /sourdough loaf/i }));
    });
    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveTextContent(
      /sourdough loaf/i,
    );

    // Re-pick the Selling Option for Product A (the Selling Option A→B transition itself
    // is covered independently, on a Product with two real options, by the dedicated test
    // below — this test's own scope is the Product round trip).
    await openSelectAndPick(/selling option/i, /6-pack/i);

    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "2" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[])[0];
    expect(line.product_id).toBe("p1");
    expect(line.selling_option_id).toBe("so1");

    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("performs a genuine Selling Option A→B transition on the same Product: commits, closes, reopens correctly, and sends Option B in the final payload", async () => {
    // Select Fix Final Micro-Hardening §3 — the prior version of this coverage only
    // *claimed* an A→B Selling Option transition in its title/comment; the actual
    // fixture (PRODUCT_WITH_OPTION) has exactly one Selling Option, so no such
    // transition was ever performed. This test uses a local, two-active-option Product
    // fixture (not a change to the shared PRODUCT_WITH_OPTION, to avoid affecting any
    // other test's expectations) and genuinely performs the A→B switch.
    const PRODUCT_A_WITH_TWO_OPTIONS = {
      ...PRODUCT_WITH_OPTION,
      selling_options: [
        PRODUCT_WITH_OPTION.selling_options[0], // so1, "6-pack" — Option A
        {
          id: "so-b",
          product_id: "p1",
          name: "12-pack",
          quantity_units: "12.000000",
          price: "22.00",
          packaging_cost: "0.00",
          sort_order: 1,
          is_active: true,
          version: 1,
        }, // Option B
      ],
    };
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let capturedBody: Record<string, unknown> | null = null;
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              { id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_A_WITH_TWO_OPTIONS),
      },
      {
        method: "POST",
        pattern: /\/api\/v1\/orders$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          return jsonResponse(201, ORDER);
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, ORDER),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    // 1: choose the Product.
    await openSelectAndPick(/^product$/i, /sourdough loaf/i);

    // 2-3: select Selling Option A ("6-pack"); verify it commits and the popup closes.
    await openSelectAndPick(/selling option/i, /^6-pack$/i);
    expect(screen.getByRole("combobox", { name: /selling option/i })).toHaveTextContent(
      /6-pack/i,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    // 4: reopen Selling Option — the list must show both real options, not stale/dupe.
    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /selling option/i }));
    });
    const optionsBeforeB = await screen.findAllByRole("option");
    expect(optionsBeforeB.map((o) => o.textContent)).toEqual(["6-pack", "12-pack"]);

    // 5-7: select Selling Option B ("12-pack"); verify B commits and the popup closes.
    await act(async () => {
      fireEvent.pointerDown(screen.getByRole("option", { name: /12-pack/i }), {
        pointerType: "mouse",
      });
      fireEvent.click(screen.getByRole("option", { name: /12-pack/i }));
    });
    expect(screen.getByRole("combobox", { name: /selling option/i })).toHaveTextContent(
      /12-pack/i,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);

    // 8-9: reopen once more — list still correct, not duplicated/stale (still exactly
    // the two real options, "6-pack" not somehow removed or doubled).
    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /selling option/i }));
    });
    const optionsAfterB = await screen.findAllByRole("option");
    expect(optionsAfterB.map((o) => o.textContent)).toEqual(["6-pack", "12-pack"]);
    await act(async () => {
      fireEvent.keyDown(document.body, { key: "Escape" });
    });

    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "2" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    // 11: the final outgoing payload carries Option B, not the earlier Option A.
    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[])[0];
    expect(line.product_id).toBe("p1");
    expect(line.selling_option_id).toBe("so-b");

    // 10: no duplicate-key React warning at any point in this A→B transition.
    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("Custom Quantity Product Select commits and closes normally, including a repeated A→B→A switch", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockTwoProductCatalog();

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom quantity line/i }));
    });

    await openSelectAndPick(/^line 1 product$/i, /sourdough loaf/i);
    expect(screen.getByRole("combobox", { name: /^line 1 product$/i })).toHaveTextContent(
      /sourdough loaf/i,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/fulfillment-facing notes/i), {
      target: { value: "still interactive" },
    });
    expect(screen.getByLabelText(/fulfillment-facing notes/i)).toHaveValue("still interactive");

    await openSelectAndPick(/^line 1 product$/i, /chocolate chip cookie/i);
    expect(screen.getByRole("combobox", { name: /^line 1 product$/i })).toHaveTextContent(
      /chocolate chip cookie/i,
    );

    await openSelectAndPick(/^line 1 product$/i, /sourdough loaf/i);
    expect(screen.getByRole("combobox", { name: /^line 1 product$/i })).toHaveTextContent(
      /sourdough loaf/i,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);

    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("Customer/Guest Select is unaffected (already had a distinct fallback key) and continues to work through repeated switches", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              { id: "c1", name: "Ada Lovelace", phone: null, email: null, is_active: true, version: 1 },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await openSelectAndPick(/^customer$/i, /^ada lovelace$/i);
    expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(
      "Ada Lovelace",
    );
    await openSelectAndPick(/^customer$/i, /guest \/ walk-in/i);
    expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(
      /guest \/ walk-in/i,
    );
    await openSelectAndPick(/^customer$/i, /^ada lovelace$/i);
    expect(screen.getByRole("combobox", { name: /^customer$/i })).toHaveTextContent(
      "Ada Lovelace",
    );

    expect(findDuplicateKeyWarnings(errorSpy)).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it("does not show the zero-active-options message while the Product detail query is still loading", async () => {
    // Select Fix Final Micro-Hardening §2 — a controllable/delayed mock for the Product
    // detail request specifically, so the loading-state assertion below is a real
    // mid-flight snapshot rather than something that merely happens to pass because the
    // mocked promise already resolved synchronously by the time it's checked.
    let resolveProductDetail!: (response: Response) => void;
    const productDetailPromise = new Promise<Response>((resolve) => {
      resolveProductDetail = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init?: RequestInit) => {
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.endsWith("/api/v1/auth/me") && method === "GET") {
          return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
        }
        if (/\/api\/v1\/customers\?/.test(url)) {
          return Promise.resolve(jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }));
        }
        if (/\/api\/v1\/products\?/.test(url)) {
          return Promise.resolve(
            jsonResponse(200, {
              items: [
                { id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
              ],
              total: 1,
              limit: 200,
              offset: 0,
            }),
          );
        }
        if (/\/api\/v1\/products\/p1$/.test(url)) {
          return productDetailPromise;
        }
        throw new Error(`Unhandled fetch: ${method} ${url}`);
      }),
    );

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    // Picking the Product only needs the already-resolved active-Products list — the
    // detail request (which the zero-options message depends on) is still pending.
    await openSelectAndPick(/^product$/i, /sourdough loaf/i);
    expect(screen.getByRole("combobox", { name: /^product$/i })).toHaveTextContent(
      /sourdough loaf/i,
    );

    expect(
      screen.queryByText(/no active selling options available for this product/i),
    ).not.toBeInTheDocument();

    // Resolve now so the pending promise doesn't leak into a later test.
    await act(async () => {
      resolveProductDetail(jsonResponse(200, { ...PRODUCT_WITH_OPTION, selling_options: [] }));
    });
  });

  it("shows the zero-active-options message once the Product detail query succeeds with zero active Selling Options, and keeps the line non-submittable", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              { id: "p1", name: "Sourdough Loaf", product_type: "PRODUCED", is_active: true, version: 1, has_active_selling_option: true },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, { ...PRODUCT_WITH_OPTION, selling_options: [] }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    await openSelectAndPick(/^product$/i, /sourdough loaf/i);

    expect(
      await screen.findByText(/no active selling options available for this product/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /selling option/i })).toBeDisabled();

    // Attempting to save without a valid Selling Option must not submit — the line
    // schema's own requirement blocks it (Checkpoint-3 correction 1's superRefine).
    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "1" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    expect(await screen.findByText(/selling option is required/i)).toBeInTheDocument();
  });
});

// Manual Acceptance Pricing/UX Correction — the human tester's Tests B-D findings.
// Reads the "Total" row of the Customer-facing total preview card specifically (not
// "Subtotal", which shows the identical figure whenever adjustment/tax are both zero —
// the default — and would make `getByText` ambiguous).
function getPreviewTotalText(): string {
  const dt = screen.getByText("Total");
  return dt.nextElementSibling?.textContent ?? "";
}

const PRODUCT_COOKIE_SIX_PACK = {
  id: "p-cookie",
  name: "Chocolate Chip Cookie",
  description: null,
  product_type: "PRODUCED",
  default_packaging_cost: "0.00",
  can_reuse_surplus: false,
  default_surplus_usable_days: null,
  is_active: true,
  version: 1,
  selling_options: [
    {
      id: "so-six-pack",
      product_id: "p-cookie",
      name: "Six Pack",
      quantity_units: "6.000000",
      price: "40.00",
      packaging_cost: "0.00",
      sort_order: 0,
      is_active: true,
      version: 1,
    },
  ],
};

describe("Order Entry — STANDARD_OPTION Product eligibility (Manual Acceptance Pricing/UX §1)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("excludes an active Product with zero active Selling Options from a new Standard Option pick, but still offers a Product that has one", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-no-opt",
                name: "No-Option Product",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: false,
              },
              {
                id: "p1",
                name: "Sourdough Loaf",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 2,
            limit: 200,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /^product$/i }));
    });

    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Sourdough Loaf"]);
    expect(screen.queryByRole("option", { name: /no-option product/i })).not.toBeInTheDocument();
  });

  it("still offers a Product with zero active Selling Options to a Custom Quantity line", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-no-opt",
                name: "No-Option Product",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: false,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom quantity line/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /^line 1 product$/i }));
    });

    expect(
      await screen.findByRole("option", { name: /no-option product/i }),
    ).toBeInTheDocument();
  });

  it("keeps an existing carried-forward Standard Option source (now ineligible for a new pick) visibly displayed", async () => {
    const PRODUCT_NOW_OPTIONLESS = {
      ...PRODUCT_WITH_OPTION,
      selling_options: [{ ...PRODUCT_WITH_OPTION.selling_options[0], is_active: false }],
    };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        // p1 excluded from the active-eligible list — it's now optionless.
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p1$/,
        respond: () => jsonResponse(200, PRODUCT_NOW_OPTIONLESS),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    const productTrigger = await screen.findByRole("combobox", { name: /^product$/i });
    await waitFor(() => expect(productTrigger).toHaveTextContent(/sourdough loaf/i));
  });

  it("mixed catalog: offers only the two eligible Products (A and C), never the ineligible one (B)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-a",
                name: "Product A",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
              {
                id: "p-b",
                name: "Product B",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: false,
              },
              {
                id: "p-c",
                name: "Product C",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 3,
            limit: 200,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: /^product$/i }));
    });

    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Product A", "Product C"]);
  });

  it("empty eligible set: keeps the Product control rendered (disabled) with a clear zero-eligible message, never a silently empty/vanished control", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-no-opt",
                name: "No-Option Product",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: false,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    const productTrigger = await screen.findByRole("combobox", { name: /^product$/i });
    // The control itself must still be rendered and visible — never removed/hidden —
    // just correctly disabled with an explanatory message, so the seller isn't left
    // staring at what looks like a broken/missing control (the exact End-to-End Fix
    // failure this regression test guards against).
    await waitFor(() => expect(productTrigger).toBeDisabled());
    expect(
      await screen.findByText(/no products with active selling options are available/i),
    ).toBeInTheDocument();
  });

  it("loading: never claims zero eligible Products while the Product list request is still in flight", async () => {
    let resolveProducts!: (value: Response) => void;
    const productsPromise = new Promise<Response>((resolve) => {
      resolveProducts = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init?: RequestInit) => {
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.endsWith("/api/v1/auth/me") && method === "GET") {
          return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
        }
        if (/\/api\/v1\/customers\?/.test(url)) {
          return Promise.resolve(jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }));
        }
        if (/\/api\/v1\/products\?/.test(url)) {
          return productsPromise;
        }
        throw new Error(`Unhandled fetch: ${method} ${url}`);
      }),
    );

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    const productTrigger = screen.getByRole("combobox", { name: /^product$/i });
    expect(productTrigger).toHaveTextContent(/loading products/i);
    expect(
      screen.queryByText(/no products with active selling options are available/i),
    ).not.toBeInTheDocument();

    await act(async () => {
      resolveProducts(
        jsonResponse(200, {
          items: [
            {
              id: "p1",
              name: "Sourdough Loaf",
              product_type: "PRODUCED",
              is_active: true,
              version: 1,
              has_active_selling_option: true,
            },
          ],
          total: 1,
          limit: 200,
          offset: 0,
        }),
      );
      await productsPromise;
    });

    await waitFor(() => expect(productTrigger).not.toHaveTextContent(/loading/i));
  });

  it("error: shows a visible Product-load error, never a false zero-eligible message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init?: RequestInit) => {
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.endsWith("/api/v1/auth/me") && method === "GET") {
          return Promise.resolve(AUTHENTICATED_ME_RESPONSE.clone());
        }
        if (/\/api\/v1\/customers\?/.test(url)) {
          return Promise.resolve(jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }));
        }
        if (/\/api\/v1\/products\?/.test(url)) {
          return Promise.resolve(
            jsonResponse(500, {
              error: { code: "INTERNAL_SERVER_ERROR", message: "Server error.", issues: [] },
            }),
          );
        }
        throw new Error(`Unhandled fetch: ${method} ${url}`);
      }),
    );

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });

    expect(await screen.findByText(/unable to load products/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/no products with active selling options are available/i),
    ).not.toBeInTheDocument();
  });
});

describe("Order Entry — STANDARD_OPTION customer-total preview (Manual Acceptance Pricing/UX §2 BLOCKER)", () => {
  afterEach(() => vi.unstubAllGlobals());

  function mockCookieCatalog(extra: Handler[] = []) {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-cookie",
                name: "Chocolate Chip Cookie",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p-cookie$/,
        respond: () => jsonResponse(200, PRODUCT_COOKIE_SIX_PACK),
      },
      ...extra,
    ]);
  }

  it("new line, blank override: previews package_quantity × the live selected-option price ($40 × 3 = $120)", async () => {
    mockCookieCatalog();
    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await openSelectAndPick(/^product$/i, /chocolate chip cookie/i);
    await openSelectAndPick(/selling option/i, /six pack/i);
    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "3" } });

    await waitFor(() => expect(getPreviewTotalText()).toBe("$120.00"));
  });

  it("explicit override wins: $3 override × 3 packages previews $9", async () => {
    mockCookieCatalog();
    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await openSelectAndPick(/^product$/i, /chocolate chip cookie/i);
    await openSelectAndPick(/selling option/i, /six pack/i);
    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText(/price override/i), { target: { value: "3.00" } });

    await waitFor(() => expect(getPreviewTotalText()).toBe("$9.00"));
  });

  it("package_quantity means packages, not individual units: quantity 30 × $3 override previews $90", async () => {
    mockCookieCatalog();
    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await openSelectAndPick(/^product$/i, /chocolate chip cookie/i);
    await openSelectAndPick(/selling option/i, /six pack/i);
    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText(/price override/i), { target: { value: "3.00" } });

    await waitFor(() => expect(getPreviewTotalText()).toBe("$90.00"));
  });

  it("retained line, same source, blank override: preserves the stored $40 snapshot even after the live catalog price changes", async () => {
    const EDIT_ORDER_COOKIE = {
      ...ORDER,
      lines: [
        {
          ...ORDER_LINE,
          product_id: "p-cookie",
          selling_option_id: "so-six-pack",
          package_quantity: "1.000000",
          charged_unit_price_snapshot: "40.00",
        },
      ],
    };
    const PRODUCT_COOKIE_PRICE_CHANGED = {
      ...PRODUCT_COOKIE_SIX_PACK,
      selling_options: [{ ...PRODUCT_COOKIE_SIX_PACK.selling_options[0], price: "999.00" }],
    };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER_COOKIE) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-cookie",
                name: "Chocolate Chip Cookie",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p-cookie$/,
        respond: () => jsonResponse(200, PRODUCT_COOKIE_PRICE_CHANGED),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    // The override input starts blank (Checkpoint-3 correction 2) and the source is
    // untouched — the preview must show the stored $40 snapshot, never the live $999.
    await waitFor(() => expect(getPreviewTotalText()).toBe("$40.00"));
  });

  it("retained line, actual source change, blank override: uses the newly selected option's live catalog price", async () => {
    const EDIT_ORDER_COOKIE = {
      ...ORDER,
      lines: [
        {
          ...ORDER_LINE,
          product_id: "p-cookie",
          selling_option_id: "so-six-pack",
          package_quantity: "1.000000",
          charged_unit_price_snapshot: "40.00",
        },
      ],
    };
    const PRODUCT_COOKIE_TWO_OPTIONS = {
      ...PRODUCT_COOKIE_SIX_PACK,
      selling_options: [
        PRODUCT_COOKIE_SIX_PACK.selling_options[0],
        {
          id: "so-dozen",
          product_id: "p-cookie",
          name: "Dozen",
          quantity_units: "12.000000",
          price: "70.00",
          packaging_cost: "0.00",
          sort_order: 1,
          is_active: true,
          version: 1,
        },
      ],
    };
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER_COOKIE) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p-cookie",
                name: "Chocolate Chip Cookie",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\/p-cookie$/,
        respond: () => jsonResponse(200, PRODUCT_COOKIE_TWO_OPTIONS),
      },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    await waitFor(() => expect(getPreviewTotalText()).toBe("$40.00"));

    await openSelectAndPick(/selling option/i, /^dozen$/i);

    // package_quantity is still 1 (untouched) — the new Dozen option's own live price
    // ($70) now applies, not the old stored Six Pack snapshot ($40).
    await waitFor(() => expect(getPreviewTotalText()).toBe("$70.00"));
  });

  it("saved Order with a blank override receives the correct authoritative server total (server remains authoritative)", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    mockCookieCatalog([
      {
        method: "POST",
        pattern: /\/api\/v1\/orders$/,
        respond: (_url, init) => {
          capturedBody = JSON.parse((init?.body as string) ?? "{}");
          // The server is authoritative: it returns its own computed total,
          // independent of whatever the client preview happened to show.
          return jsonResponse(201, { ...ORDER, final_total: "120.00", subtotal: "120.00" });
        },
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/orders\/o1$/,
        respond: () => jsonResponse(200, { ...ORDER, final_total: "120.00", subtotal: "120.00" }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add standard option line/i }));
    });
    await openSelectAndPick(/^product$/i, /chocolate chip cookie/i);
    await openSelectAndPick(/selling option/i, /six pack/i);
    fireEvent.change(screen.getByLabelText(/package quantity/i), { target: { value: "3" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    });

    await waitFor(() => expect(capturedBody).not.toBeNull());
    const line = (capturedBody!.lines as Record<string, unknown>[])[0];
    // Blank override submitted as null/omitted — the server derives the real price.
    expect(line.charged_unit_price == null || line.charged_unit_price === "").toBe(true);
    expect(await screen.findByRole("heading", { name: "ORD-ABCDEF012345" })).toBeInTheDocument();
    // The server's own authoritative total ($120.00, computed server-side from the
    // catalog price the client never sent) is what's actually displayed post-save —
    // not merely that the save succeeded. Targeted at the "Total" <dd> specifically,
    // since "Subtotal" shows the identical figure whenever adjustment/tax are zero.
    await screen.findByText("Total");
    expect(screen.getByText("Total").nextElementSibling?.textContent).toBe("$120.00");
  });
});

describe("Order Entry — CUSTOM_QUANTITY and CUSTOM_ITEM preview accuracy (Manual Acceptance Pricing/UX §5/§6/§9)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("Custom Quantity: 30 units, $55 agreed price, $5 packaging override previews exactly $55 (packaging never inflates the customer total)", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom quantity line/i }));
    });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText(/agreed line price/i), { target: { value: "55.00" } });
    fireEvent.change(screen.getByLabelText(/packaging cost override/i), {
      target: { value: "5.00" },
    });

    await waitFor(() => expect(getPreviewTotalText()).toBe("$55.00"));
  });

  it("Custom Item: preview is quantity × unit price; internal direct-cost estimate and active-time do not change it", async () => {
    mockFetchRouter([
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
    ]);

    renderAt("/app/orders/new");
    await screen.findByRole("heading", { name: /new order/i });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add custom item line/i }));
    });
    fireEvent.change(screen.getByLabelText(/^description$/i), { target: { value: "Cake topper" } });
    fireEvent.change(screen.getByLabelText(/^quantity$/i), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText(/unit price/i), { target: { value: "10.00" } });

    await waitFor(() => expect(getPreviewTotalText()).toBe("$20.00"));

    fireEvent.change(screen.getByLabelText(/internal direct-cost estimate/i), {
      target: { value: "999.00" },
    });
    fireEvent.change(screen.getByLabelText(/estimated active time/i), { target: { value: "500" } });

    // Still $20 — the huge internal cost/time values never factor into the customer
    // preview.
    expect(getPreviewTotalText()).toBe("$20.00");
  });
});

describe("Order Entry — persisted quantity display trims trailing zeros (Manual Acceptance Pricing/UX §4)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders a persisted package_quantity of 3.000000 as 3 in the edit form", async () => {
    mockFetchRouter([
      { method: "GET", pattern: /\/api\/v1\/orders\/o1$/, respond: () => jsonResponse(200, EDIT_ORDER) },
      {
        method: "GET",
        pattern: /\/api\/v1\/customers\?/,
        respond: () => jsonResponse(200, { items: [], total: 0, limit: 200, offset: 0 }),
      },
      {
        method: "GET",
        pattern: /\/api\/v1\/products\?/,
        respond: () =>
          jsonResponse(200, {
            items: [
              {
                id: "p1",
                name: "Sourdough Loaf",
                product_type: "PRODUCED",
                is_active: true,
                version: 1,
                has_active_selling_option: true,
              },
            ],
            total: 1,
            limit: 200,
            offset: 0,
          }),
      },
      { method: "GET", pattern: /\/api\/v1\/products\/p1$/, respond: () => jsonResponse(200, PRODUCT_WITH_OPTION) },
      { method: "GET", pattern: /\/api\/v1\/products\/p2$/, respond: () => jsonResponse(200, PRODUCT_P2) },
    ]);

    renderAt("/app/orders/o1/edit");
    await screen.findByRole("heading", { name: /edit order/i });

    // EDIT_ORDER's STANDARD_OPTION line-1 has package_quantity "2.000000" — assert the
    // displayed input value is the trimmed "2", not the raw persisted string.
    expect(screen.getByLabelText(/package quantity/i)).toHaveValue("2");
  });
});
