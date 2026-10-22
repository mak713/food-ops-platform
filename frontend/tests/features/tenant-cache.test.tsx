// Authenticated tenant-cache boundary (Phase 11 Revision 2; release-relevant).
//
// Proves that no Business A data can be read by Business B after logout, account deletion,
// confirmed session expiry, or a login as another principal — including while B's own
// responses are still in flight — without recreating the stale-`me` / redirect-loop bugs that
// `clearMe()` (resetQueries) exists to avoid.

import {
  MutationObserver as QueryMutationObserver,
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { routes } from "../../src/app/routes";
import { AuthProvider } from "../../src/features/auth/AuthContext";
import {
  createCacheBoundary,
  isTenantQuery,
  purgeTenantQueries,
  refreshTenantQueriesAfterTimezoneChange,
} from "../../src/features/auth/tenantCache";

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ME_KEY = ["auth", "me"];

describe("purgeTenantQueries / refreshTenantQueriesAfterTimezoneChange (unit)", () => {
  it("removes every non-auth query and leaves the auth query untouched", () => {
    const qc = new QueryClient();
    qc.setQueryData(ME_KEY, { user: { id: "u1" } });
    for (const root of [
      "analytics",
      "customers",
      "dashboard",
      "ingredients",
      "orders",
      "production-requirements",
      "production-runs",
      "products",
      "purchased-inventory",
      "settings",
      "shopping-list",
      "surplus",
      "some-future-feature",
    ]) {
      qc.setQueryData([root, "x"], { secret: "A" });
    }
    purgeTenantQueries(qc);
    expect(
      qc
        .getQueryCache()
        .getAll()
        .map((q) => q.queryKey),
    ).toEqual([ME_KEY]);
    expect(qc.getQueryData(ME_KEY)).toEqual({ user: { id: "u1" } });
  });

  it("purgeTenantQueries alone drops settled mutation results and leaves an in-flight one (the boundary, not the purge, holds the gate for it)", async () => {
    const qc = new QueryClient();
    const done = qc.getMutationCache().build(qc, { mutationFn: async () => ({ secret: "A" }) });
    await done.execute(undefined);
    let release: () => void = () => {};
    const pending = qc.getMutationCache().build(qc, {
      mutationFn: () => new Promise<void>((resolve) => (release = resolve)),
    });
    const running = pending.execute(undefined);
    await new Promise((resolve) => setTimeout(resolve, 0)); // let mutationFn start
    expect(qc.getMutationCache().getAll()).toHaveLength(2);
    purgeTenantQueries(qc);
    expect(qc.getMutationCache().getAll()).toEqual([pending]);
    release();
    await running;
  });

  it("classifies by root key only", () => {
    const qc = new QueryClient();
    qc.setQueryData(["auth", "me"], 1);
    qc.setQueryData(["auth", "other"], 1);
    qc.setQueryData(["customers"], 1);
    const flags = qc
      .getQueryCache()
      .getAll()
      .map((q) => [q.queryKey.join("/"), isTenantQuery(q)]);
    expect(flags).toEqual([
      ["auth/me", false],
      ["auth/other", false],
      ["customers", true],
    ]);
  });

  it("timezone change: inactive tenant queries are removed, active ones refetched, auth refreshed", async () => {
    const qc = new QueryClient();
    qc.setQueryData(ME_KEY, { user: { id: "u1" } });
    qc.setQueryData(["analytics", "overview", "old-zone"], "OLD");
    qc.setQueryData(["dashboard"], "OLD");
    qc.setQueryData(["orders", "list"], "OLD");
    qc.setQueryData(["some-future-feature"], "OLD");

    // A query the visible screen is observing right now.
    let activeFetches = 0;
    const observer = new QueryObserver(qc, {
      queryKey: ["shopping-list", "visible"],
      queryFn: async () => {
        activeFetches += 1;
        return "fresh";
      },
    });
    const unsubscribe = observer.subscribe(() => {});
    await waitFor(() => expect(activeFetches).toBe(1));

    refreshTenantQueriesAfterTimezoneChange(qc);

    const remaining = qc
      .getQueryCache()
      .getAll()
      .map((q) => q.queryKey.join("/"))
      .sort();
    expect(remaining).toEqual(["auth/me", "shopping-list/visible"]);
    await waitFor(() => expect(activeFetches).toBe(2)); // active query refetched
    expect(qc.getQueryState(ME_KEY)?.isInvalidated).toBe(true);
    unsubscribe();
  });
});

describe("createCacheBoundary", () => {
  const deferredMutation = (qc: QueryClient, onSuccess?: () => void, fail = false) => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    const mutation = qc.getMutationCache().build(qc, {
      mutationFn: async () => {
        await held;
        if (fail) throw new Error("boom");
        return { secret: "A" };
      },
      onSuccess,
      onSettled: () => {
        qc.setQueryData(["settings", "business"], { writtenBy: "A" }); // hook-level, any outcome
      },
    });
    const running = mutation.execute(undefined).catch(() => {});
    return { mutation, release, running };
  };
  const tick = () => new Promise((resolve) => setTimeout(resolve, 20));

  it("notifies subscribers only when the owner or the outstanding count actually changes", () => {
    const boundary = createCacheBoundary(new QueryClient());
    const listener = vi.fn();
    const unsubscribe = boundary.subscribe(listener);
    boundary.syncPrincipal(null);
    expect(listener).not.toHaveBeenCalled();
    boundary.syncPrincipal("u1");
    boundary.syncPrincipal("u1");
    expect(listener).toHaveBeenCalledTimes(1);
    expect(boundary.getSnapshot()).toEqual({ owner: "u1", outstanding: 0 });
    unsubscribe();
    boundary.syncPrincipal("u2");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("tracks a mutation pending at the boundary and only reports 0 outstanding after its callbacks ran and a final purge removed what they wrote", async () => {
    const qc = new QueryClient();
    const boundary = createCacheBoundary(qc);
    boundary.syncPrincipal("A");
    qc.setQueryData(["customers"], { owner: "A" });
    const { mutation, release, running } = deferredMutation(qc);
    await tick();

    boundary.syncPrincipal("B"); // A -> B directly
    expect(boundary.getSnapshot()).toEqual({ owner: "B", outstanding: 1 });
    expect(qc.getQueryData(["customers"])).toBeUndefined(); // queries purged immediately
    expect(qc.getMutationCache().getAll()).toContain(mutation); // pending one retained, tracked

    release();
    await running;
    await tick();
    expect(boundary.getSnapshot()).toEqual({ owner: "B", outstanding: 0 });
    expect(qc.getQueryData(["settings", "business"])).toBeUndefined(); // its late write was purged
    expect(qc.getMutationCache().getAll()).not.toContain(mutation); // result not retained
  });

  it("also clears the barrier when the old mutation fails, and keeps it closed across A -> nobody -> B", async () => {
    const qc = new QueryClient();
    const boundary = createCacheBoundary(qc);
    boundary.syncPrincipal("A");
    const { release, running } = deferredMutation(qc, undefined, true);
    await tick();

    boundary.syncPrincipal(null);
    expect(boundary.getSnapshot()).toEqual({ owner: null, outstanding: 1 });
    boundary.syncPrincipal("B"); // null -> B must not forget the debt
    expect(boundary.getSnapshot()).toEqual({ owner: "B", outstanding: 1 });

    release();
    await running;
    await tick();
    expect(boundary.getSnapshot()).toEqual({ owner: "B", outstanding: 0 });
    expect(qc.getQueryData(["settings", "business"])).toBeUndefined();
  });

  it("counts a mutation once across overlapping boundaries (A -> B -> C)", async () => {
    const qc = new QueryClient();
    const boundary = createCacheBoundary(qc);
    boundary.syncPrincipal("A");
    const { release, running } = deferredMutation(qc);
    await tick();
    boundary.syncPrincipal("B");
    boundary.syncPrincipal("C");
    expect(boundary.getSnapshot()).toEqual({ owner: "C", outstanding: 1 });
    release();
    await running;
    await tick();
    expect(boundary.getSnapshot()).toEqual({ owner: "C", outstanding: 0 });
  });

  it("has nothing to wait for when no mutation is pending", () => {
    const boundary = createCacheBoundary(new QueryClient());
    boundary.syncPrincipal("A");
    boundary.syncPrincipal("B");
    expect(boundary.getSnapshot()).toEqual({ owner: "B", outstanding: 0 });
  });
});

// ------------------------------------------------------------------------ app-level fake

interface Deferred {
  promise: Promise<Response>;
  resolve: (response: Response) => void;
}

function deferred(): Deferred {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((r) => (resolve = r));
  return { promise, resolve };
}

const CUSTOMERS_BY_USER: Record<string, string> = {
  uA: "Alpha-Secret-Customer",
  uB: "Bravo-Visible-Customer",
};

function customersBody(userId: string) {
  return {
    items: [
      {
        id: `c-${userId}`,
        name: CUSTOMERS_BY_USER[userId],
        phone: null,
        email: null,
        is_active: true,
        version: 1,
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
  };
}

function createTenantServer() {
  const state = {
    current: "uA" as "uA" | "uB" | null,
    delayCustomersFor: null as "uA" | "uB" | null,
    delayed: [] as Deferred[],
    requests: [] as string[],
  };
  const meResponse = () =>
    state.current === null
      ? json(401, {
          error: {
            code: "AUTH_UNAUTHENTICATED",
            message: "Authentication is required.",
            issues: [],
          },
        })
      : json(200, {
          user: {
            id: state.current,
            name: state.current === "uA" ? "Alice" : "Bob",
            email: `${state.current}@example.com`,
          },
          business: {
            id: `biz-${state.current}`,
            name: state.current === "uA" ? "Alpha Bakery" : "Bravo Bakery",
            timezone: "America/New_York",
          },
        });

  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    state.requests.push(`${method} ${path}`);
    if (path === "/api/v1/auth/me") return Promise.resolve(meResponse());
    if (path === "/api/v1/auth/logout") {
      state.current = null;
      return Promise.resolve(json(200, { status: "ok" }));
    }
    if (path === "/api/v1/auth/login") {
      const body = JSON.parse(String(init?.body));
      state.current = body.email.startsWith("b") ? "uB" : "uA";
      return Promise.resolve(meResponse());
    }
    if (path === "/api/v1/account" && method === "DELETE") {
      state.current = null;
      return Promise.resolve(json(200, { status: "ok" }));
    }
    if (path.startsWith("/api/v1/customers?")) {
      if (state.current === null) {
        return Promise.resolve(
          json(401, {
            error: {
              code: "AUTH_UNAUTHENTICATED",
              message: "Authentication is required.",
              issues: [],
            },
          }),
        );
      }
      const owner = state.current;
      if (state.delayCustomersFor === owner) {
        const d = deferred();
        state.delayed.push(d);
        return d.promise.then(() => json(200, customersBody(owner)));
      }
      return Promise.resolve(json(200, customersBody(owner)));
    }
    throw new Error(`Unhandled fetch: ${method} ${path}`);
  });
  return { state, fetchMock };
}

let tenantServer: ReturnType<typeof createTenantServer>;
let queryClient: QueryClient;

function renderApp(initialPath: string) {
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

/** Records whether `needle` was ever present in the DOM or the query cache after `armed`. */
function watchForLeak(needle: string) {
  const sightings: string[] = [];
  let armed = false;
  const check = (where: string) => {
    if (armed && document.body.textContent?.includes(needle)) sightings.push(`dom:${where}`);
  };
  const observer = new MutationObserver(() => check("mutation"));
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  return {
    arm: () => {
      armed = true;
    },
    sightings,
    stop: () => observer.disconnect(),
    cacheHolds: () =>
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map((q) => q.state.data),
      ).includes(needle),
  };
}

const cacheKeys = () =>
  queryClient
    .getQueryCache()
    .getAll()
    .map((q) => String(q.queryKey[0]));

beforeEach(() => {
  tenantServer = createTenantServer();
  vi.stubGlobal("fetch", tenantServer.fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Clicks "Log out" and lets the mutation's async chain (POST -> clearMe -> refetch ->
 * navigate) settle, exactly as the existing App.test logout test does. */
async function logout() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /log out/i }));
    await new Promise((resolve) => setTimeout(resolve, 60));
  });
  expect(screen.getByRole("heading", { name: /log in/i })).toBeInTheDocument();
}

async function submitLogin(email: string) {
  fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/password/i), {
    target: { value: "correct horse battery staple" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^log in$/i }));
  });
}

describe("tenant data never crosses a principal boundary", () => {
  it("logout then login as B (B's responses delayed) never shows or retains A's data", async () => {
    const router = renderApp("/app/customers");
    expect(await screen.findByText("Alpha-Secret-Customer")).toBeInTheDocument();
    expect(cacheKeys()).toContain("customers");

    const leak = watchForLeak("Alpha-Secret-Customer");
    leak.arm();

    await logout();
    leak.sightings.length = 0; // A's data was legitimately on screen until the tree unmounted
    await waitFor(() => expect(cacheKeys().every((k) => k === "auth")).toBe(true));
    expect(leak.cacheHolds()).toBe(false);

    // B logs in; B's customer list will be slow
    tenantServer.state.delayCustomersFor = "uB";
    await act(async () => {
      await router.navigate("/login?next=%2Fapp%2Fcustomers");
    });
    await submitLogin("bob@example.com");

    // wait until B's list request is actually in flight
    await waitFor(() => expect(tenantServer.state.delayed.length).toBeGreaterThan(0));
    expect(document.body.textContent).not.toContain("Alpha-Secret-Customer");
    expect(leak.cacheHolds()).toBe(false);

    await act(async () => {
      tenantServer.state.delayed.forEach((d) => d.resolve(json(200, {})));
    });
    expect(await screen.findByText("Bravo-Visible-Customer")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("Alpha-Secret-Customer");
    expect(leak.sightings).toEqual([]);
    expect(leak.cacheHolds()).toBe(false);
    expect(router.state.location.pathname).toBe("/app/customers");
    leak.stop();
  });

  it("does not loop or resurrect the authenticated user after logout", async () => {
    const router = renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    await logout();
    const meCallsAfterLogout = tenantServer.state.requests.filter(
      (r) => r === "GET /api/v1/auth/me",
    ).length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 150));
    });
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.getByRole("heading", { name: /log in/i })).toBeInTheDocument();
    // settled: no runaway /me refetching
    const meCallsLater = tenantServer.state.requests.filter(
      (r) => r === "GET /api/v1/auth/me",
    ).length;
    expect(meCallsLater - meCallsAfterLogout).toBeLessThanOrEqual(1);
  });

  it("a confirmed session expiry (401 on /me) purges tenant data and redirects to login", async () => {
    const router = renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    const leak = watchForLeak("Alpha-Secret-Customer");

    tenantServer.state.current = null; // the session silently expired server-side
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ME_KEY });
    });
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    await waitFor(() => expect(cacheKeys().every((k) => k === "auth")).toBe(true));
    expect(leak.cacheHolds()).toBe(false);
    leak.stop();
  });

  it("a principal change without logout (another tab signed in as B) swaps the tenant cache", async () => {
    renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    const leak = watchForLeak("Alpha-Secret-Customer");

    tenantServer.state.current = "uB"; // cookies were replaced in another tab
    tenantServer.state.delayCustomersFor = "uB";
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ME_KEY });
    });
    // while B's list is loading, A's rows must be gone from the screen and the cache
    await waitFor(() => expect(tenantServer.state.delayed.length).toBeGreaterThan(0));
    expect(document.body.textContent).not.toContain("Alpha-Secret-Customer");
    expect(leak.cacheHolds()).toBe(false);

    await act(async () => {
      tenantServer.state.delayed.forEach((d) => d.resolve(json(200, {})));
    });
    expect(await screen.findByText("Bravo-Visible-Customer")).toBeInTheDocument();
    expect(leak.cacheHolds()).toBe(false);
    leak.stop();
  });

  it("account deletion leaves only auth queries in the cache and lands on login", async () => {
    const router = renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    await act(async () => {
      await router.navigate("/app/settings?area=account");
    });
    fireEvent.change(
      await screen.findByLabelText("Current password", { selector: "#delete_current_password" }),
      {
        target: { value: "correct horse battery staple" },
      },
    );
    fireEvent.change(screen.getByLabelText(/to confirm/i), { target: { value: "Alpha Bakery" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));

    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();
    await waitFor(() => expect(cacheKeys().every((k) => k === "auth")).toBe(true));
    expect(document.body.textContent).not.toContain("Alpha-Secret-Customer");
  });

  it("keeps the existing clearMe() behavior: the auth query is reset, not removed", async () => {
    renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    await logout();
    // the observed auth query still exists and reports the unauthenticated state
    expect(queryClient.getQueryCache().find({ queryKey: ME_KEY })).toBeDefined();
    await waitFor(() => expect(queryClient.getQueryData(ME_KEY)).toBeUndefined());
  });
});

// ------------------------------------------------- old-principal mutations crossing a boundary
//
// A tenant mutation started by Business A can still be pending when the authenticated
// principal changes. TanStack runs a mutation's *hook-level* callbacks (the `onSuccess` given to
// `useMutation`, e.g. useBusinessSettingsSave's `setQueryData(...)`) when it settles even after
// the component unmounted and even if the mutation was removed from the MutationCache. Those
// callbacks must not be able to write A's data into the cache that Business B can see.

const LATE_MARKER = "LATE-ALPHA-MARKER";
const LATE_PAYLOAD = {
  items: [{ id: "late", name: LATE_MARKER, phone: null, email: null, is_active: true, version: 1 }],
  total: 1,
  limit: 50,
  offset: 0,
};

/** A real TanStack mutation (via MutationObserver, exactly what useMutation drives) whose
 * hook-level onSuccess writes A-owned data into a tenant query, held pending until released. */
function startHeldTenantMutation(qc: QueryClient, key: readonly unknown[]) {
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  let callbackRan = false;
  const observer = new QueryMutationObserver(qc, {
    mutationFn: async () => {
      await held;
      return LATE_PAYLOAD;
    },
    onSuccess: (data) => {
      callbackRan = true;
      qc.setQueryData(key, data); // production-equivalent: useBusinessSettingsSave's onSuccess
    },
  });
  void observer.mutate().catch(() => {});
  return {
    release,
    callbackRan: () => callbackRan,
    settled: () =>
      qc
        .getMutationCache()
        .getAll()
        .every((m) => m.state.status !== "pending"),
  };
}

const customersKey = () => {
  const found = queryClient.getQueryCache().findAll({ queryKey: ["customers"] });
  expect(found.length).toBeGreaterThan(0);
  return found[0].queryKey;
};

const cacheHolds = (needle: string) =>
  JSON.stringify([
    ...queryClient
      .getQueryCache()
      .getAll()
      .map((q) => q.state.data),
    ...queryClient
      .getMutationCache()
      .getAll()
      .map((m) => m.state.data),
  ]).includes(needle);

describe("an old-principal tenant mutation that is still pending when the principal changes", () => {
  it("A -> B without logout: B stays gated until the old mutation settles; its late write never reaches B", async () => {
    renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    const key = customersKey();
    const late = startHeldTenantMutation(queryClient, key);
    const leak = watchForLeak(LATE_MARKER);
    leak.arm();

    // another tab signs in as B; /me now reports B directly (no unauthenticated render)
    tenantServer.state.current = "uB";
    tenantServer.state.delayCustomersFor = "uB";
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ME_KEY });
    });

    // A's tree is gone, B's is NOT open: the old mutation can still run callbacks
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Bravo Bakery")).not.toBeInTheDocument(); // B's shell not rendered
    expect(tenantServer.state.delayed).toHaveLength(0); // no B tenant query started yet
    expect(late.settled()).toBe(false);

    // release A's old mutation: its hook-level onSuccess executes while B is still gated
    await act(async () => {
      late.release();
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(late.callbackRan()).toBe(true);

    // only now may B open; B's own responses are still held back
    await waitFor(() => expect(tenantServer.state.delayed.length).toBeGreaterThan(0));
    expect(await screen.findByText("Bravo Bakery")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(LATE_MARKER);
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    expect(cacheHolds("Alpha-Secret-Customer")).toBe(false);

    await act(async () => {
      tenantServer.state.delayed.forEach((d) => d.resolve(json(200, {})));
    });
    expect(await screen.findByText("Bravo-Visible-Customer")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(LATE_MARKER);
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    expect(leak.sightings).toEqual([]);
    leak.stop();
  });

  it("logout boundary: the old mutation settles AFTER B has logged in; B waits and never sees it", async () => {
    const router = renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    const late = startHeldTenantMutation(queryClient, customersKey());
    const leak = watchForLeak(LATE_MARKER);
    leak.arm();

    await logout();
    tenantServer.state.delayCustomersFor = "uB";
    await act(async () => {
      await router.navigate("/login?next=%2Fapp%2Fcustomers");
    });
    await submitLogin("bob@example.com");

    // B is authenticated but gated while A's mutation can still run its callbacks
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 150));
    });
    expect(screen.queryByText("Bravo Bakery")).not.toBeInTheDocument();
    expect(tenantServer.state.delayed).toHaveLength(0);

    await act(async () => {
      late.release();
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(late.callbackRan()).toBe(true);
    await waitFor(() => expect(tenantServer.state.delayed.length).toBeGreaterThan(0));
    expect(document.body.textContent).not.toContain(LATE_MARKER);
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    await act(async () => {
      tenantServer.state.delayed.forEach((d) => d.resolve(json(200, {})));
    });
    expect(await screen.findByText("Bravo-Visible-Customer")).toBeInTheDocument();
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    expect(leak.sightings).toEqual([]);
    leak.stop();
  });

  it("confirmed 401: a mutation settling after expiry and a later login as B leaves nothing of A", async () => {
    const router = renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    const late = startHeldTenantMutation(queryClient, customersKey());

    tenantServer.state.current = null; // the session silently expired
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ME_KEY });
    });
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    await act(async () => {
      late.release(); // A's mutation settles while nobody is signed in
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(late.callbackRan()).toBe(true);

    await submitLogin("bob@example.com");
    expect(await screen.findByText("Bravo Bakery")).toBeInTheDocument();
    // checked at the instant B's shell opens, before any B data has been fetched over it
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    expect(document.body.textContent).not.toContain(LATE_MARKER);
    await act(async () => {
      await router.navigate("/app/customers");
    });
    expect(await screen.findByText("Bravo-Visible-Customer")).toBeInTheDocument();
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    expect(document.body.textContent).not.toContain(LATE_MARKER);
  });

  it("once the boundary completes: no mutation result is retained, auth query intact, only B tenant data", async () => {
    renderApp("/app/customers");
    await screen.findByText("Alpha-Secret-Customer");
    const late = startHeldTenantMutation(queryClient, customersKey());
    tenantServer.state.current = "uB";
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ME_KEY });
    });
    await act(async () => {
      late.release();
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(await screen.findByText("Bravo-Visible-Customer")).toBeInTheDocument();

    expect(late.settled()).toBe(true);
    const leftoverMutations = queryClient
      .getMutationCache()
      .getAll()
      .filter((m) => m.state.status !== "pending");
    expect(leftoverMutations.map((m) => m.state.data)).not.toContain(LATE_PAYLOAD);
    expect(cacheHolds(LATE_MARKER)).toBe(false);
    expect(cacheHolds("Alpha-Secret-Customer")).toBe(false);
    expect(queryClient.getQueryData<{ user: { id: string } }>(ME_KEY)?.user.id).toBe("uB");
    for (const q of queryClient.getQueryCache().getAll()) {
      if (String(q.queryKey[0]) !== "auth") {
        expect(JSON.stringify(q.state.data ?? null)).not.toContain("Alpha");
      }
    }
  });
});
