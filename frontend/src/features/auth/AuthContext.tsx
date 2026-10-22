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
//
// Phase 11 (tenant-cache boundary): clearMe() above is untouched. In addition, this provider
// owns the rule that no tenant data may outlive the principal that fetched it. `principalId`
// is the authenticated user's id, or null once logged out / deleted / confirmed-401. When it
// changes away from a non-null principal, every non-auth query is purged (tenantCache.ts) —
// after the protected tree has been unmounted: `tenantCacheReady` is false for exactly the
// render in which the principal differs from the principal the cache was last purged for, so
// ProtectedRoute renders no screen (and therefore no tenant observer exists) until the purge
// effect has run and recorded the new cache owner. That ordering is what makes the purge
// safe and what prevents a previous tenant's data from flashing for the next one.
//
// A tenant mutation still pending across the boundary can run hook-level callbacks (e.g. a
// `setQueryData`) after the purge, so the cache is also not "ready" while any such mutation is
// outstanding; it is purged again when each one settles (see createCacheBoundary).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { ApiError } from "../../api/client";
import { authApi, type MeResponse } from "./api";
import { createCacheBoundary } from "./tenantCache";

export const ME_QUERY_KEY = ["auth", "me"] as const;

interface AuthContextValue {
  me: MeResponse | undefined;
  isLoading: boolean;
  isAuthenticated: boolean;
  isUnauthenticated: boolean;
  /** False while the tenant cache still belongs to a different principal (see header). */
  tenantCacheReady: boolean;
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

  const principalId = query.data && !isUnauthenticated ? query.data.user.id : null;
  const [boundary] = useState(() => createCacheBoundary(queryClient));
  const { owner: cacheOwner, outstanding } = useSyncExternalStore(
    boundary.subscribe,
    boundary.getSnapshot,
  );

  useEffect(() => {
    boundary.syncPrincipal(principalId);
  }, [principalId, boundary]);

  const value: AuthContextValue = {
    me: query.data,
    isLoading: query.isLoading,
    isAuthenticated: Boolean(query.data) && !isUnauthenticated,
    isUnauthenticated,
    tenantCacheReady: principalId === null || (cacheOwner === principalId && outstanding === 0),
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
