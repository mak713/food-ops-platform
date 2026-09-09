import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, ME_QUERY_KEY } from "../src/features/auth/AuthContext";
import { routes } from "../src/app/routes";

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const UNAUTHENTICATED_ME_RESPONSE = () =>
  jsonResponse(401, {
    error: { code: "AUTH_UNAUTHENTICATED", message: "Authentication is required.", issues: [] },
  });

const AUTHENTICATED_ME_RESPONSE = () =>
  jsonResponse(200, {
    user: { id: "u1", name: "Test Owner", email: "owner@example.com" },
    business: { id: "b1", name: "Test Bakery", timezone: "America/New_York" },
  });

const SERVER_ERROR_ME_RESPONSE = () =>
  jsonResponse(500, {
    error: { code: "INTERNAL_SERVER_ERROR", message: "An unexpected error occurred.", issues: [] },
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

describe("app router — unauthenticated", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(UNAUTHENTICATED_ME_RESPONSE())),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the login page at /login", () => {
    renderAt("/login");
    expect(screen.getByRole("heading", { name: /log in/i })).toBeInTheDocument();
  });

  it("renders the signup page at /signup", () => {
    renderAt("/signup");
    expect(screen.getByRole("heading", { name: /create your account/i })).toBeInTheDocument();
  });

  it("redirects / to /login", () => {
    renderAt("/");
    expect(screen.getByRole("heading", { name: /log in/i })).toBeInTheDocument();
  });

  it("redirects an unauthenticated visit to /app/dashboard back to /login", async () => {
    renderAt("/app/dashboard");
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();
  });
});

describe("app router — authenticated", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(AUTHENTICATED_ME_RESPONSE())),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the dashboard under the protected /app layout", async () => {
    renderAt("/app/dashboard");
    expect(await screen.findByRole("heading", { name: /dashboard/i })).toBeInTheDocument();
    expect(screen.getAllByText(/Test Bakery/).length).toBeGreaterThan(0);
  });

  it("does not render the login form once authenticated", async () => {
    renderAt("/login");
    expect(await screen.findByRole("heading", { name: /dashboard/i })).toBeInTheDocument();
  });
});

// Checkpoint 2 review, issue 3: only a confirmed 401 from /me may be classified as
// "logged out." A 500 (or network failure) must not redirect to /login — that would
// misrepresent a transient failure as a session expiry.
describe("app router — /me check fails with a non-401 error", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(SERVER_ERROR_ME_RESPONSE())),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not redirect to /login on a 500 from /me", async () => {
    renderAt("/app/dashboard");
    expect(
      await screen.findByText(/couldn't confirm your session/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /log in/i })).not.toBeInTheDocument();
  });

  it("does not render the protected dashboard either", async () => {
    renderAt("/app/dashboard");
    await screen.findByText(/couldn't confirm your session/i);
    expect(screen.queryByRole("heading", { name: /dashboard/i })).not.toBeInTheDocument();
  });
});

describe("app router — /me check fails with a network error", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.reject(new TypeError("Network request failed"))),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not redirect to /login on a network failure from /me", async () => {
    renderAt("/app/dashboard");
    expect(
      await screen.findByText(/couldn't confirm your session/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /log in/i })).not.toBeInTheDocument();
  });
});

// Checkpoint 2 review follow-up: TanStack Query retains the previous successful `data`
// in cache even after a later refetch fails, so a confirmed 401 on refetch can coexist
// with stale authenticated `data` from an earlier successful check. The 401 must win.
describe("app router — a confirmed 401 refetch overrides stale cached auth data", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("stops rendering the dashboard and redirects to /login once /me refetches a 401", async () => {
    let callCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => {
        callCount += 1;
        // Step 1: the first /me call succeeds — authenticated data gets cached.
        // Step 2: every call after that returns 401 (session expired/revoked).
        return Promise.resolve(
          callCount === 1 ? AUTHENTICATED_ME_RESPONSE() : UNAUTHENTICATED_ME_RESPONSE(),
        );
      }),
    );

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(routes, { initialEntries: ["/app/dashboard"] });
    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <RouterProvider router={router} />
        </AuthProvider>
      </QueryClientProvider>,
    );

    // Confirms the cached-authenticated starting state the bug depends on.
    expect(await screen.findByRole("heading", { name: /dashboard/i })).toBeInTheDocument();

    // Step 2, triggered explicitly: a subsequent /me refetch resolves to 401 while
    // TanStack Query still holds the previous successful result in `data`.
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
    });

    // Step 3: the confirmed 401 must take precedence over the stale cached user.
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /dashboard/i })).not.toBeInTheDocument();
  });
});

// Manual Test #3 finding: after a successful logout, the browser stayed on
// /app/dashboard showing the authenticated dashboard instead of immediately reaching
// /login — clearMe()'s setQueryData(key, undefined) was a no-op (TanStack Query treats
// an undefined value as "no update"), so the stale authenticated /me data never
// actually cleared and LoginPage's own "already authenticated" check bounced the user
// straight back to the dashboard.
describe("app router — logout immediately clears auth state and redirects", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("stops rendering the dashboard and shows the login page right after logout, with no refresh", async () => {
    let loggedOut = false;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const path = typeof url === "string" ? url : String(url);
        if (path.includes("/auth/logout")) {
          loggedOut = true;
          return Promise.resolve(jsonResponse(200, { status: "ok" }));
        }
        // Matches real backend behavior: /me succeeds until logout revokes the
        // session, then returns 401 — the test doesn't need this to actually fire for
        // the fix to pass, but it keeps the mock realistic if a refetch does happen.
        return Promise.resolve(
          loggedOut ? UNAUTHENTICATED_ME_RESPONSE() : AUTHENTICATED_ME_RESPONSE(),
        );
      }),
    );

    renderAt("/app/dashboard");

    // 1. Authenticated state.
    expect(await screen.findByRole("heading", { name: /dashboard/i })).toBeInTheDocument();

    // 2. Successful logout. The mutation's async chain (POST /logout -> onSuccess ->
    // clearMe()'s query reset+refetch -> navigate) spans several promise hops; wrapping
    // the click and a settle tick in one `act` flushes all of them together so the
    // assertions below observe the fully-settled state rather than racing it (confirmed
    // stable well within this window via a diagnostic timing probe during development —
    // the real running app shows no perceptible delay at all).
    const logoutButton = screen.getByRole("button", { name: /log out/i });
    await act(async () => {
      fireEvent.click(logoutButton);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // 3 & 4. Dashboard no longer rendered; login page appears immediately — no refresh.
    expect(screen.getByRole("heading", { name: /log in/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /dashboard/i })).not.toBeInTheDocument();
  });
});

// Manual Test #6 finding: after being redirected to /login?next=%2Fapp%2Faccount and
// logging in successfully, the app landed on /app/dashboard instead of /app/account.
// Root cause: LoginPage's "already authenticated" render-time redirect always targeted
// /app/dashboard, unconditionally of `next`, and could win a race against the login
// mutation's own next-aware navigate() call (both fire once isAuthenticated flips true).
describe("app router — login honors a safe next destination", () => {
  function mockLoginAndMe() {
    // Tracks real login state: the initial mount's GET /me (before submitting the form)
    // must return 401, but the login mutation's own post-success refetch of GET /me
    // must then return the authenticated user — otherwise ProtectedRoute correctly (but
    // misleadingly, for this test's purposes) treats it as still-unauthenticated and
    // bounces back to /login, which looks identical to the bug this test guards against.
    let loggedIn = false;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init?: RequestInit) => {
        const path = typeof url === "string" ? url : String(url);
        if (path.includes("/auth/login") && init?.method === "POST") {
          loggedIn = true;
          return Promise.resolve(AUTHENTICATED_ME_RESPONSE());
        }
        return Promise.resolve(
          loggedIn ? AUTHENTICATED_ME_RESPONSE() : UNAUTHENTICATED_ME_RESPONSE(),
        );
      }),
    );
  }

  async function submitLoginForm() {
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "owner@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "correct horse battery staple" },
    });
    const submitButton = screen.getByRole("button", { name: /^log in$/i });
    await act(async () => {
      fireEvent.click(submitButton);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("navigates to the requested next path (/app/account) after a successful login", async () => {
    mockLoginAndMe();
    renderAt("/login?next=%2Fapp%2Faccount");
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();

    await submitLoginForm();

    expect(screen.getByRole("heading", { name: "Account" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /dashboard/i })).not.toBeInTheDocument();
  });

  it("navigates to the dashboard when no next is present", async () => {
    mockLoginAndMe();
    renderAt("/login");
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();

    await submitLoginForm();

    expect(screen.getByRole("heading", { name: /dashboard/i })).toBeInTheDocument();
  });

  it("falls back to the dashboard for an unsafe external next value, never leaving the app", async () => {
    mockLoginAndMe();
    renderAt("/login?next=https%3A%2F%2Fevil.example.com");
    expect(await screen.findByRole("heading", { name: /log in/i })).toBeInTheDocument();

    await submitLoginForm();

    expect(screen.getByRole("heading", { name: /dashboard/i })).toBeInTheDocument();
    expect(window.location.href).not.toContain("evil.example.com");
  });
});
