// Settings feature tests (Phase 11): the four areas, URL-synced tabs, per-area saves with
// optimistic concurrency, validation (client UX + server-authoritative), the timezone
// confirmation, margin ratio<->percent conversion, strict response handling, owner profile,
// password change, controlled account deletion, and the /app/account compatibility redirect.
// Uses a small stateful fake API so saves, version bumps and conflicts behave like the real one.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";

function json(status: number, body: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
  });
}

interface ServerSettings {
  id: string;
  name: string;
  business_type: string | null;
  contact_phone: string | null;
  contact_email: string | null;
  address_text: string | null;
  timezone: string;
  default_labor_rate: string;
  target_contribution_margin: string | null;
  fulfillment_warning_minutes: number;
  shopping_horizon_days: number;
  version: number;
}

function baseSettings(): ServerSettings {
  return {
    id: "b1",
    name: "Test Bakery",
    business_type: null,
    contact_phone: null,
    contact_email: null,
    address_text: null,
    timezone: "America/New_York",
    default_labor_rate: "0.00",
    target_contribution_margin: null,
    fulfillment_warning_minutes: 60,
    shopping_horizon_days: 7,
    version: 1,
  };
}

interface Recorded {
  method: string;
  path: string;
  body: Record<string, unknown> | null;
}

function createServer() {
  const state = {
    settings: baseSettings(),
    user: { id: "u1", name: "Test Owner", email: "owner@example.com" },
    requests: [] as Recorded[],
    settingsResponse: null as (() => Response) | null,
    patchResponse: null as ((body: Record<string, unknown>) => Response | null) | null,
    deleted: false,
  };
  const me = () =>
    state.deleted
      ? json(401, {
          error: {
            code: "AUTH_UNAUTHENTICATED",
            message: "Authentication is required.",
            issues: [],
          },
        })
      : json(200, {
          user: { ...state.user },
          business: {
            id: "b1",
            name: state.settings.name,
            timezone: state.settings.timezone,
          },
        });

  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
    state.requests.push({ method, path, body });

    if (path === "/api/v1/auth/me" && method === "GET") return Promise.resolve(me());
    if (path === "/api/v1/settings" && method === "GET") {
      return Promise.resolve(
        state.settingsResponse ? state.settingsResponse() : json(200, state.settings),
      );
    }
    if (path === "/api/v1/settings/business" && method === "PATCH" && body) {
      const override = state.patchResponse?.(body);
      if (override) return Promise.resolve(override);
      if (body.version !== state.settings.version) {
        return Promise.resolve(
          json(409, {
            error: {
              code: "STALE_VERSION",
              message: "This record changed since you opened it.",
              issues: [],
            },
          }),
        );
      }
      const { version: _version, ...changes } = body;
      void _version;
      state.settings = {
        ...state.settings,
        ...changes,
        version: state.settings.version + 1,
      } as ServerSettings;
      return Promise.resolve(json(200, state.settings));
    }
    if (path === "/api/v1/settings/owner" && method === "PATCH" && body) {
      state.user = { ...state.user, name: String(body.name) };
      return Promise.resolve(json(200, state.user));
    }
    if (path === "/api/v1/auth/password/change" && method === "POST") {
      return Promise.resolve(json(200, { status: "ok" }));
    }
    if (path === "/api/v1/account" && method === "DELETE") {
      state.deleted = true;
      return Promise.resolve(json(200, { status: "ok" }));
    }
    throw new Error(`Unhandled fetch: ${method} ${path}`);
  });

  return { state, fetchMock };
}

type FakeServer = ReturnType<typeof createServer>;
let server: FakeServer;
let queryClient: QueryClient;

function renderAt(initialPath: string) {
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return router;
}

const patches = () =>
  server.state.requests.filter(
    (r) => r.method === "PATCH" && r.path === "/api/v1/settings/business",
  );

beforeEach(() => {
  server = createServer();
  vi.stubGlobal("fetch", server.fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Settings page structure", () => {
  it("shows four areas with the Business Profile selected by default and a Settings nav link", async () => {
    renderAt("/app/settings");
    expect(await screen.findByRole("heading", { name: "Settings", level: 1 })).toBeInTheDocument();
    const tabs = within(screen.getByRole("tablist", { name: /settings areas/i })).getAllByRole(
      "tab",
    );
    expect(tabs.map((t) => t.textContent)).toEqual([
      "Business Profile",
      "Costing & Pricing",
      "Operations",
      "Account",
    ]);
    expect(screen.getByRole("tab", { name: "Business Profile" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      await screen.findByRole("heading", { name: "Business Profile", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/app/settings");
    expect(screen.queryByRole("link", { name: "Account" })).not.toBeInTheDocument();
  });

  it("initializes the form from the server and opens the area named in ?area=", async () => {
    server.state.settings = {
      ...baseSettings(),
      name: "Sweet Co",
      contact_email: "Hello@Example.com",
      default_labor_rate: "18.50",
      target_contribution_margin: "0.625000",
      fulfillment_warning_minutes: 45,
      shopping_horizon_days: 3,
    };
    renderAt("/app/settings?area=costing");
    expect(await screen.findByLabelText(/default labor rate/i)).toHaveValue("18.50");
    expect(screen.getByLabelText(/target contribution margin/i)).toHaveValue("62.5");
    expect(screen.getByRole("tab", { name: "Costing & Pricing" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("changes area with the keyboard and keeps the area in the URL", async () => {
    const router = renderAt("/app/settings");
    const first = await screen.findByRole("tab", { name: "Business Profile" });
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(
      await screen.findByRole("heading", { name: "Costing & Pricing", level: 2 }),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe("?area=costing");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Costing & Pricing" }), { key: "End" });
    expect(await screen.findByRole("heading", { name: "Account", level: 2 })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Account" }), { key: "Home" });
    expect(
      await screen.findByRole("heading", { name: "Business Profile", level: 2 }),
    ).toBeInTheDocument();
  });

  it("falls back to Business Profile for an unknown area value", async () => {
    renderAt("/app/settings?area=bogus");
    expect(
      await screen.findByRole("heading", { name: "Business Profile", level: 2 }),
    ).toBeInTheDocument();
  });
});

describe("Business Profile", () => {
  it("saves only the changed fields with the loaded version, shows Saved, and updates the header", async () => {
    renderAt("/app/settings");
    const name = await screen.findByLabelText("Business name");
    const save = screen.getByRole("button", { name: /save changes/i });
    expect(save).toBeDisabled();

    fireEvent.change(name, { target: { value: "  Renamed Bakery  " } });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    expect(patches()).toHaveLength(1);
    expect(patches()[0].body).toEqual({ version: 1, name: "Renamed Bakery" });
    // header business name follows the refreshed `me`
    await waitFor(() => expect(screen.getAllByText("Renamed Bakery").length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: /save changes/i })).toBeDisabled();
  });

  it("sends null for a cleared optional field and trims text", async () => {
    server.state.settings = {
      ...baseSettings(),
      business_type: "Home bakery",
      contact_phone: "555",
    };
    renderAt("/app/settings");
    fireEvent.change(await screen.findByLabelText("Business type"), { target: { value: "   " } });
    fireEvent.change(screen.getByLabelText("Contact phone"), { target: { value: " 555-0100 " } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({
      version: 1,
      business_type: null,
      contact_phone: "555-0100",
    });
  });

  it("validates required name and contact-email shape on the client without a request", async () => {
    renderAt("/app/settings");
    fireEvent.change(await screen.findByLabelText("Business name"), { target: { value: "   " } });
    fireEvent.change(screen.getByLabelText("Contact email"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByText("Business name is required")).toBeInTheDocument();
    expect(screen.getByText("Enter a valid email address")).toBeInTheDocument();
    expect(patches()).toHaveLength(0);
  });

  it("accepts any typed timezone (not limited to the suggestions) behind a confirmation", async () => {
    renderAt("/app/settings");
    const tz = await screen.findByLabelText("Business timezone");
    // a closed <select> would make this impossible; the control is a free-text input
    expect(tz.tagName).toBe("INPUT");
    expect(document.getElementById("timezone-suggestions")?.tagName).toBe("DATALIST");

    fireEvent.change(tz, { target: { value: "US/Pacific" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    const dialog = await screen.findByRole("alertdialog");
    for (const phrase of [
      /existing fulfillment dates and times/i,
      /today/i,
      /dashboard and production timing/i,
      /shopping list business-local dates/i,
      /analytics/i,
      /not rewritten/i,
    ]) {
      expect(within(dialog).getByText(phrase)).toBeInTheDocument();
    }
    expect(patches()).toHaveLength(0); // nothing saved before confirmation

    fireEvent.click(within(dialog).getByRole("button", { name: "Change timezone" }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({ version: 1, timezone: "US/Pacific" });
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
  });

  it("drops every cached tenant query derived under the old timezone after a timezone change", async () => {
    renderAt("/app/settings");
    const tz = await screen.findByLabelText("Business timezone");
    // screens visited earlier in the session (not currently displayed)
    for (const key of [
      ["dashboard"],
      ["analytics", "overview", "LAST_30_DAYS"],
      ["shopping-list", "default"],
      ["orders", "list"],
      ["production-runs"],
      ["surplus"],
      ["products"],
      ["purchased-inventory"],
    ]) {
      queryClient.setQueryData(key, { derivedUnder: "America/New_York" });
    }
    fireEvent.change(tz, { target: { value: "Asia/Kolkata" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Change timezone",
      }),
    );
    await waitFor(() => expect(patches()).toHaveLength(1));
    await waitFor(() => {
      const roots = queryClient
        .getQueryCache()
        .getAll()
        .map((q) => String(q.queryKey[0]));
      expect(roots.filter((r) => r !== "auth" && r !== "settings")).toEqual([]);
    });
  });

  it("does not drop other cached queries for a change that is not a timezone change", async () => {
    renderAt("/app/settings");
    const name = await screen.findByLabelText("Business name");
    queryClient.setQueryData(["dashboard"], { keep: true });
    fireEvent.change(name, { target: { value: "Just A Rename" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(queryClient.getQueryData(["dashboard"])).toEqual({ keep: true });
  });

  it("does not save a timezone change when the confirmation is cancelled", async () => {
    renderAt("/app/settings");
    fireEvent.change(await screen.findByLabelText("Business timezone"), {
      target: { value: "Asia/Kolkata" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep current timezone" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(patches()).toHaveLength(0);
    expect(screen.getByLabelText("Business timezone")).toHaveValue("Asia/Kolkata");
  });

  it("does not ask for confirmation when the timezone is unchanged", async () => {
    renderAt("/app/settings");
    fireEvent.change(await screen.findByLabelText("Business name"), { target: { value: "Other" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("shows the server's message under the timezone field when it rejects the value", async () => {
    server.state.patchResponse = () =>
      json(422, {
        error: {
          code: "VALIDATION_ERROR",
          message: "Request validation failed.",
          issues: [
            {
              severity: "ERROR",
              code: "value_error",
              message: "Enter a valid IANA timezone, for example America/New_York.",
              field: "timezone",
              resource: null,
              details: {},
            },
          ],
        },
      });
    renderAt("/app/settings");
    fireEvent.change(await screen.findByLabelText("Business timezone"), {
      target: { value: "Mars/Phobos" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Change timezone",
      }),
    );
    expect(
      await screen.findByText("Enter a valid IANA timezone, for example America/New_York."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Business timezone")).toHaveValue("Mars/Phobos");
  });

  it("shows a generic alert for an unexpected server failure and keeps the edits", async () => {
    server.state.patchResponse = () =>
      json(500, {
        error: {
          code: "INTERNAL_SERVER_ERROR",
          message: "An unexpected error occurred.",
          issues: [],
        },
      });
    renderAt("/app/settings");
    fireEvent.change(await screen.findByLabelText("Business name"), {
      target: { value: "Edited" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("An unexpected error occurred.");
    expect(screen.getByLabelText("Business name")).toHaveValue("Edited");
  });

  it("offers stale-edit recovery on a 409 and re-bases the form on the fresh version", async () => {
    renderAt("/app/settings");
    const name = await screen.findByLabelText("Business name");
    // someone else saves first
    server.state.settings = { ...server.state.settings, name: "Changed Elsewhere", version: 2 };

    fireEvent.change(name, { target: { value: "My Edit" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByText(/this record changed since you opened it/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Business name")).toHaveValue("Changed Elsewhere"),
    );
    expect(screen.queryByText(/this record changed since you opened it/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Second Try" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(2));
    expect(patches()[1].body).toEqual({ version: 2, name: "Second Try" });
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
  });

  it("submits the version it was loaded with, not a version learned from a background refetch", async () => {
    renderAt("/app/settings");
    const name = await screen.findByLabelText("Business name");
    fireEvent.change(name, { target: { value: "Based On v1" } });
    // the query cache is updated behind the form's back (another tab, a refetch...)
    server.state.settings = { ...server.state.settings, version: 5 };
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ["settings", "business"] });
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByText(/this record changed since you opened it/i)).toBeInTheDocument();
    expect(patches()[0].body).toEqual({ version: 1, name: "Based On v1" });
  });
});

describe("Costing & Pricing", () => {
  it("converts the margin percentage to an exact ratio and normalizes the labor rate", async () => {
    renderAt("/app/settings?area=costing");
    fireEvent.change(await screen.findByLabelText(/default labor rate/i), {
      target: { value: "20" },
    });
    fireEvent.change(screen.getByLabelText(/target contribution margin/i), {
      target: { value: "62.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({
      version: 1,
      default_labor_rate: "20.00",
      target_contribution_margin: "0.625000",
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    expect(screen.getByLabelText(/target contribution margin/i)).toHaveValue("62.5");
    expect(screen.getByLabelText(/default labor rate/i)).toHaveValue("20.00");
  });

  it("clears the target with a blank margin (null) and rejects out-of-range percentages", async () => {
    server.state.settings = { ...baseSettings(), target_contribution_margin: "0.600000" };
    renderAt("/app/settings?area=costing");
    const margin = await screen.findByLabelText(/target contribution margin/i);
    expect(margin).toHaveValue("60");

    fireEvent.change(margin, { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByText(/from 0 up to \(but not including\) 100/i)).toBeInTheDocument();
    expect(patches()).toHaveLength(0);

    fireEvent.change(margin, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({ version: 1, target_contribution_margin: null });
  });

  it("rejects an invalid labor rate on the client", async () => {
    renderAt("/app/settings?area=costing");
    fireEvent.change(await screen.findByLabelText(/default labor rate/i), {
      target: { value: "-5" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByText(/enter an amount such as 18.50/i)).toBeInTheDocument();
    expect(patches()).toHaveLength(0);
  });

  it("explains that a labor-rate change applies to future work and never rewrites history", async () => {
    renderAt("/app/settings?area=costing");
    expect(
      await screen.findByText(/applies to future production starts and subsequent recalculations/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/are not rewritten/i)).toBeInTheDocument();
  });
});

describe("Operations", () => {
  it("saves warning minutes and the Shopping List horizon", async () => {
    renderAt("/app/settings?area=operations");
    fireEvent.change(await screen.findByLabelText(/fulfillment warning window/i), {
      target: { value: "90" },
    });
    fireEvent.change(screen.getByLabelText(/default shopping list horizon/i), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({
      version: 1,
      fulfillment_warning_minutes: 90,
      shopping_horizon_days: 3,
    });
  });

  it("accepts the largest storable value and rejects 0, negatives, decimals and overflow", async () => {
    renderAt("/app/settings?area=operations");
    const minutes = await screen.findByLabelText(/fulfillment warning window/i);
    for (const bad of ["0", "-5", "1.5", "abc", "2147483648", ""]) {
      fireEvent.change(minutes, { target: { value: bad } });
      fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
      expect(
        await screen.findByText(/whole number of minutes from 1 to 2,147,483,647/i),
      ).toBeInTheDocument();
    }
    expect(patches()).toHaveLength(0);

    // no invented product maximum: a week and far beyond are fine
    fireEvent.change(minutes, { target: { value: "2147483647" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].body).toEqual({ version: 1, fulfillment_warning_minutes: 2147483647 });
  });

  it("offers exactly the 3-day and 7-day horizons", async () => {
    renderAt("/app/settings?area=operations");
    const horizon = await screen.findByLabelText(/default shopping list horizon/i);
    expect(
      within(horizon)
        .getAllByRole("option")
        .map((o) => o.getAttribute("value")),
    ).toEqual(["3", "7"]);
  });
});

describe("Settings load handling", () => {
  it("refuses a malformed /settings response instead of rendering partial UI", async () => {
    // e.g. an unrelated /me-shaped body: production code must not tolerate it
    server.state.settingsResponse = () =>
      json(200, {
        user: { id: "u1", name: "Test Owner", email: "owner@example.com" },
        business: { id: "b1", name: "Test Bakery", timezone: "America/New_York" },
      });
    renderAt("/app/settings");
    expect(await screen.findByText(/we couldn't load your settings/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Business name")).not.toBeInTheDocument();
  });

  it("shows a retryable error for a server failure and recovers", async () => {
    let calls = 0;
    server.state.settingsResponse = () => {
      calls += 1;
      return calls === 1
        ? json(500, {
            error: {
              code: "INTERNAL_SERVER_ERROR",
              message: "An unexpected error occurred.",
              issues: [],
            },
          })
        : json(200, server.state.settings);
    };
    renderAt("/app/settings");
    expect(await screen.findByText("An unexpected error occurred.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByLabelText("Business name")).toHaveValue("Test Bakery");
  });
});

describe("Account area", () => {
  it("redirects /app/account to the Account area without requesting Business Settings", async () => {
    const router = renderAt("/app/account");
    expect(await screen.findByRole("heading", { name: "Account" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/app/settings");
    expect(router.state.location.search).toBe("?area=account");
    expect(server.state.requests.some((r) => r.path === "/api/v1/settings")).toBe(false);
  });

  it("edits the owner name, keeps the email read-only, and refreshes the signed-in user", async () => {
    renderAt("/app/settings?area=account");
    const name = await screen.findByLabelText("Your name");
    expect(name).toHaveValue("Test Owner");
    expect(screen.getByTestId("owner-email")).toHaveTextContent("owner@example.com");
    expect(screen.queryByLabelText(/login email/i)).not.toBeInTheDocument(); // not an input

    fireEvent.change(name, { target: { value: "  New Name  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    const owner = server.state.requests.find((r) => r.path === "/api/v1/settings/owner");
    expect(owner?.body).toEqual({ name: "New Name" });
    // no version in the owner contract (intentional last-write-wins)
    expect(owner?.body).not.toHaveProperty("version");
    await waitFor(() => expect(screen.getByLabelText("Your name")).toHaveValue("New Name"));
  });

  it("changes the password through the existing endpoint", async () => {
    renderAt("/app/settings?area=account");
    fireEvent.change(
      await screen.findByLabelText("Current password", { selector: "#current_password" }),
      {
        target: { value: "old password value" },
      },
    );
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "a brand new long passphrase" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Password changed.")).toBeInTheDocument();
    const call = server.state.requests.find((r) => r.path === "/api/v1/auth/password/change");
    expect(call?.body).toEqual({
      current_password: "old password value",
      new_password: "a brand new long passphrase",
    });
  });

  it("requires the exact business name and the password before deleting, then signs out", async () => {
    renderAt("/app/settings?area=account");
    const password = await screen.findByLabelText("Current password", {
      selector: "#delete_current_password",
    });
    const confirmation = screen.getByLabelText(/to confirm/i);

    fireEvent.change(password, { target: { value: "correct horse battery staple" } });
    fireEvent.change(confirmation, { target: { value: "test bakery" } }); // wrong case
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));
    expect(
      await screen.findByText(/type "test bakery" exactly|Type "Test Bakery" exactly/i),
    ).toBeInTheDocument();
    expect(server.state.requests.some((r) => r.method === "DELETE")).toBe(false);

    fireEvent.change(confirmation, { target: { value: "Test Bakery" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();
    const del = server.state.requests.find((r) => r.method === "DELETE");
    expect(del?.body).toEqual({
      current_password: "correct horse battery staple",
      business_name_confirmation: "Test Bakery",
    });
  });

  it("keeps the deletion form and shows the server message when deletion conflicts", async () => {
    const original = server.fetchMock.getMockImplementation();
    server.fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/v1/account") && (init?.method ?? "").toUpperCase() === "DELETE") {
        return Promise.resolve(
          json(409, {
            error: {
              code: "ACCOUNT_DELETE_CONFLICT",
              message: "Nothing was deleted; please try again.",
              issues: [],
            },
          }),
        );
      }
      return original!(url, init);
    });
    renderAt("/app/settings?area=account");
    fireEvent.change(
      await screen.findByLabelText("Current password", { selector: "#delete_current_password" }),
      { target: { value: "correct horse battery staple" } },
    );
    fireEvent.change(screen.getByLabelText(/to confirm/i), { target: { value: "Test Bakery" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));
    expect(await screen.findByText("Nothing was deleted; please try again.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Account", level: 2 })).toBeInTheDocument();
  });
});
