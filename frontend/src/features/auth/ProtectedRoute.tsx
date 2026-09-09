// Gates the /app/* shell. The only place in the frontend that treats "not
// authenticated" as a reason to redirect (Phase 2 plan §14) — preserves the
// intended destination via ?next= (Spec §17.13).
//
// Redirects only on a *confirmed* 401 (isUnauthenticated) — a 500/network failure from
// the /me check is neither authenticated nor confirmed-unauthenticated, and must not be
// misclassified as "logged out" (Checkpoint 2 review, issue 3): it renders a retry state
// instead of silently bouncing the user to /login.

import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

export function ProtectedRoute() {
  const { isLoading, isAuthenticated, isUnauthenticated, refetchMe } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  }

  if (isAuthenticated) {
    return <Outlet />;
  }

  if (isUnauthenticated) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }

  return (
    <div className="flex flex-col items-center gap-3 p-8 text-sm">
      <p className="text-destructive">
        We couldn't confirm your session. Check your connection and try again.
      </p>
      <button
        type="button"
        onClick={() => refetchMe()}
        className="underline underline-offset-4"
      >
        Retry
      </button>
    </div>
  );
}
