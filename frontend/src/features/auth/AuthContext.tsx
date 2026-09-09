// Auth context: wraps a TanStack Query "who am I" check (GET /api/v1/auth/me) and
// exposes it as {me, isLoading, isAuthenticated, isUnauthenticated}. Deliberately does
// not redirect on any failure itself — that would make an unauthenticated /login visit,
// or a transient 500/network error, look identical to a real logout. The redirect-on-401
// behavior lives in ProtectedRoute, which is the one place that treats a *confirmed* 401
// from this query as "go to /login" (Phase 2 plan §14).
//
// isUnauthenticated is only true when the query's error is specifically a 401
// (AUTH_UNAUTHENTICATED) — a 500 or network failure leaves both isAuthenticated and
// isUnauthenticated false, so callers can distinguish "definitely logged out" from
// "couldn't tell" (Checkpoint 2 review, issue 3).
//
// isAuthenticated additionally requires !isUnauthenticated: TanStack Query keeps the
// previous successful `data` cached even after a later refetch fails, so a confirmed 401
// on refetch would otherwise coexist with stale `query.data` from an earlier successful
// check — making both isAuthenticated and isUnauthenticated true at once, and letting a
// consumer that happens to check isAuthenticated first render as if still logged in. The
// 401 must always win over stale cached data (Checkpoint 2 review, follow-up edge case).
//
// clearMe() uses resetQueries(), not setQueryData(key, undefined) or removeQueries():
// - setQueryData(key, undefined) is a silent no-op — TanStack Query treats an
//   updater/value that resolves to `undefined` as "no update" and leaves the existing
//   cached data untouched.
// - removeQueries() deletes the cache entry, but this query always has an active
//   observer (AuthProvider wraps the whole app and is never unmounted) — TanStack
//   Query's own docs warn that removing an *actively observed* query doesn't reliably
//   put that observer back into a correct loading/refetching state, and empirically
//   (verified against the real running app) it left the observer's exposed `data`
//   showing the stale authenticated user with no new /me request ever firing.
// Both left the stale authenticated `me` in place after logout/account-deletion, so
// isAuthenticated stayed true and LoginPage's own "already authenticated" redirect
// bounced the user straight back to /app/dashboard instead of showing the login page
// (Checkpoint 2 review, logout-state bug). resetQueries() is the API TanStack Query
// documents specifically for resetting an *active* query to its initial state and
// having it refetch — confirmed working end-to-end in the real app.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { authApi, type MeResponse } from "./api";

export const ME_QUERY_KEY = ["auth", "me"] as const;

interface AuthContextValue {
  me: MeResponse | undefined;
  isLoading: boolean;
  isAuthenticated: boolean;
  isUnauthenticated: boolean;
  refetchMe: () => Promise<unknown>;
  clearMe: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

  const query = useQuery<MeResponse>({
    queryKey: ME_QUERY_KEY,
    queryFn: authApi.me,
    retry: false,
    staleTime: 60_000,
  });

  const isUnauthenticated = query.error instanceof ApiError && query.error.status === 401;

  const value: AuthContextValue = {
    me: query.data,
    isLoading: query.isLoading,
    isAuthenticated: Boolean(query.data) && !isUnauthenticated,
    isUnauthenticated,
    refetchMe: () => queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY }),
    clearMe: () => queryClient.resetQueries({ queryKey: ME_QUERY_KEY }),
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
