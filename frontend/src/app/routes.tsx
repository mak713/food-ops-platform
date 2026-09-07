import type { RouteObject } from "react-router-dom";
import { Navigate, Outlet } from "react-router-dom";

// Phase 0 establishes only enough routing to prove React Router works: a
// top-level route and a nested layout route (the pattern the real /app/*
// shell will use from Phase 2 onward, per Spec §12.2). Every other route in
// §12.2 is added when the phase that owns it begins.

function LoginPage() {
  return (
    <div className="p-8">
      <h1 className="text-xl font-semibold">Login — coming soon</h1>
    </div>
  );
}

function AppLayout() {
  return (
    <div className="p-8">
      <Outlet />
    </div>
  );
}

function DashboardPage() {
  return <h1 className="text-xl font-semibold">Dashboard — coming soon</h1>;
}

// Exported separately from the built router (see router.tsx) so tests can
// render these routes with a MemoryRouter-backed router instead of depending
// on jsdom's window.location.
export const routes: RouteObject[] = [
  { path: "/", element: <Navigate to="/login" replace /> },
  { path: "/login", element: <LoginPage /> },
  {
    path: "/app",
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/app/dashboard" replace /> },
      { path: "dashboard", element: <DashboardPage /> },
    ],
  },
];
