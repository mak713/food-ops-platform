// Authenticated tenant-cache boundary (Phase 11 Revision 2, release-relevant).
//
// The app has ONE module-level QueryClient and none of its query keys are tenant-scoped, so
// without an explicit boundary the previous Business's cached data would survive a logout,
// an account deletion, a confirmed session expiry, or a login/signup as another principal
// and could be shown to the next user (including as a flash while the new data loads).
//
// Everything cached in the client is either an *auth* query (root key "auth": currently only
// ["auth","me"], which has its own documented reset behavior — see AuthContext.clearMe) or a
// *tenant* query (every other root: analytics, customers, dashboard, ingredients, orders,
// production-*, products, purchased-inventory, settings, shopping-list, surplus, ...). The
// predicate is deliberately generic — "everything that is not auth" — so a future feature root
// is covered without maintaining a hand-written list.
//
// These helpers never call `queryClient.clear()`: that would remove the actively observed
// ["auth","me"] query and recreate the stale-authenticated-`me` / login-redirect-loop bug that
// `clearMe()` (resetQueries) exists to avoid.

import type { Mutation, Query, QueryClient } from "@tanstack/react-query";

export const AUTH_QUERY_ROOT = "auth";
const ME_QUERY_KEY = [AUTH_QUERY_ROOT, "me"] as const;

export function isTenantQuery(query: Query): boolean {
  return query.queryKey[0] !== AUTH_QUERY_ROOT;
}

/**
 * Drops every tenant-owned cached query (and settled mutation result) and cancels anything
 * in flight. Must only run while no screen is observing tenant queries — AuthProvider
 * guarantees that by unmounting the protected tree (see `tenantCacheReady`) before calling it.
 */
export function purgeTenantQueries(queryClient: QueryClient): void {
  void queryClient.cancelQueries({ predicate: isTenantQuery });
  queryClient.removeQueries({ predicate: isTenantQuery });

  // Settled mutation results can carry tenant data (e.g. a created record's response) and
  // are retained for `gcTime` after their component unmounts. A still-pending mutation (for
  // example the logout request that triggered this purge) is left alone so it can finish.
  const mutationCache = queryClient.getMutationCache();
  for (const mutation of mutationCache.getAll()) {
    if (mutation.state.status !== "pending") {
      mutationCache.remove(mutation);
    }
  }
}

/**
 * After a successful Business timezone change, no cached screen may keep data derived under
 * the old timezone (Dashboard "today", Shopping List dates, Analytics periods, Order times...).
 * Inactive tenant queries are removed outright so they cannot re-appear with old-zone data;
 * active ones (the visible page's own queries) are refetched. `me` is refreshed so the header
 * and Order detail pick up the new zone.
 */
export function refreshTenantQueriesAfterTimezoneChange(queryClient: QueryClient): void {
  queryClient.removeQueries({ type: "inactive", predicate: isTenantQuery });
  void queryClient.invalidateQueries({ type: "active", predicate: isTenantQuery });
  void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
}

/**
 * The tenant-cache boundary: which principal the shared QueryClient's tenant data belongs to,
 * and how much *old-principal work* is still able to write into it.
 *
 * Why this exists. Purging queries is not enough on its own. A tenant mutation started by
 * Business A may still be pending when the principal changes. TanStack runs a mutation's
 * hook-level callbacks (the `onSuccess` passed to `useMutation`, e.g. `setQueryData(...)` in
 * `useBusinessSettingsSave`) when it settles — even after its component unmounted and even if
 * it was removed from the MutationCache (removal does not cancel an executing mutation). So
 * A's response could be written into the cache after B's tree opened. This boundary therefore:
 *
 *  1. on leaving a principal, purges the tenant cache and records every mutation that is
 *     still pending (`tracked`);
 *  2. reports `outstanding = tracked.size`; the app treats the cache as not ready for ANY
 *     principal while `outstanding > 0`, so no protected screen (and no tenant observer) is
 *     mounted while an old callback can still run;
 *  3. when a tracked mutation leaves `pending` — TanStack only does that AFTER all of its
 *     onSuccess/onError/onSettled callbacks have finished — purges again, so whatever the
 *     callback wrote (and the settled mutation's own result) is removed before readiness.
 *
 * It is an external store read with `useSyncExternalStore`, so changes re-render consumers
 * without calling setState inside an effect (ESLint `react-hooks/set-state-in-effect`).
 */
export interface CacheBoundarySnapshot {
  /** The principal the tenant cache currently belongs to (null = nobody). */
  owner: string | null;
  /** Old-principal mutations still pending across a boundary. */
  outstanding: number;
}

export interface CacheBoundary {
  getSnapshot: () => CacheBoundarySnapshot;
  subscribe: (listener: () => void) => () => void;
  /** Call (from an effect) whenever the authenticated principal id changes. */
  syncPrincipal: (principalId: string | null) => void;
}

export function createCacheBoundary(queryClient: QueryClient): CacheBoundary {
  let snapshot: CacheBoundarySnapshot = { owner: null, outstanding: 0 };
  const listeners = new Set<() => void>();
  const tracked = new Set<Mutation<unknown, Error, unknown, unknown>>();
  const mutationCache = queryClient.getMutationCache();

  const publish = (next: CacheBoundarySnapshot) => {
    snapshot = next;
    listeners.forEach((listener) => listener());
  };

  const track = (mutation: Mutation<unknown, Error, unknown, unknown>) => {
    if (tracked.has(mutation)) return;
    tracked.add(mutation);
    const unsubscribe = mutationCache.subscribe((event) => {
      if (event.mutation !== mutation || mutation.state.status === "pending") return;
      unsubscribe();
      tracked.delete(mutation);
      // Its callbacks have finished: remove anything they wrote, then (maybe) reopen.
      purgeTenantQueries(queryClient);
      publish({ ...snapshot, outstanding: tracked.size });
    });
  };

  return {
    getSnapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    syncPrincipal: (principalId) => {
      if (snapshot.owner === principalId) return;
      if (snapshot.owner !== null) {
        // Leaving a principal (logout, deletion, 401, or A -> B): the protected tree has been
        // unmounted by the readiness gate, so removing tenant queries is safe.
        purgeTenantQueries(queryClient);
        for (const mutation of mutationCache.getAll()) {
          if (mutation.state.status === "pending") track(mutation);
        }
      }
      publish({ owner: principalId, outstanding: tracked.size });
    },
  };
}
