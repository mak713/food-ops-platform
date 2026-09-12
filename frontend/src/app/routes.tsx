import { useMutation } from "@tanstack/react-query";
import type { RouteObject } from "react-router-dom";
import { Link, Navigate, Outlet, useNavigate } from "react-router-dom";
import { Button } from "../components/ui/button";
import { authApi } from "../features/auth/api";
import { useAuth } from "../features/auth/AuthContext";
import { AccountSettingsPage } from "../features/auth/AccountSettingsPage";
import { LoginPage } from "../features/auth/LoginPage";
import { PasswordResetConfirmPage } from "../features/auth/PasswordResetConfirmPage";
import { PasswordResetRequestPage } from "../features/auth/PasswordResetRequestPage";
import { ProtectedRoute } from "../features/auth/ProtectedRoute";
import { SignupPage } from "../features/auth/SignupPage";
import { CustomerDetailPage } from "../features/customers/CustomerDetailPage";
import { CustomerFormPage } from "../features/customers/CustomerFormPage";
import { CustomerListPage } from "../features/customers/CustomerListPage";
import { IngredientDetailPage } from "../features/ingredients/IngredientDetailPage";
import { IngredientFormPage } from "../features/ingredients/IngredientFormPage";
import { IngredientListPage } from "../features/ingredients/IngredientListPage";
import { ProductDetailPage } from "../features/products/ProductDetailPage";
import { ProductFormPage } from "../features/products/ProductFormPage";
import { ProductListPage } from "../features/products/ProductListPage";
import { RecipeCreatePage } from "../features/products/RecipeCreatePage";
import { RecipeEditPage } from "../features/products/RecipeEditPage";
import { RecipeHistoryPage } from "../features/products/RecipeHistoryPage";
import { RecipeRevisionDetailPage } from "../features/products/RecipeRevisionDetailPage";

// Phase 2 establishes the real authenticated app shell (Spec §12.2): every route under
// /app is gated by ProtectedRoute, which derives auth state from the server-managed
// session (GET /api/v1/auth/me) — never from anything client-stored. Every other module
// under §12.2 (Orders, Production, Customers, ...) is added when the phase that owns it
// begins.

function AppLayout() {
  const { me, clearMe } = useAuth();
  const navigate = useNavigate();

  const logoutMutation = useMutation({
    mutationFn: authApi.logout,
    onSuccess: () => {
      clearMe();
      navigate("/login", { replace: true });
    },
  });

  return (
    <div className="flex min-h-svh flex-col">
      <header className="flex items-center justify-between border-b border-border p-4">
        <div className="flex items-center gap-4 text-sm">
          <span className="font-semibold">{me?.business.name}</span>
          <Link to="/app/dashboard" className="text-muted-foreground hover:text-foreground">
            Dashboard
          </Link>
          <Link to="/app/customers" className="text-muted-foreground hover:text-foreground">
            Customers
          </Link>
          <Link to="/app/products" className="text-muted-foreground hover:text-foreground">
            Products
          </Link>
          <Link to="/app/inventory/ingredients" className="text-muted-foreground hover:text-foreground">
            Inventory
          </Link>
          <Link to="/app/account" className="text-muted-foreground hover:text-foreground">
            Account
          </Link>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => logoutMutation.mutate()}
          disabled={logoutMutation.isPending}
        >
          {logoutMutation.isPending ? "Logging out…" : "Log out"}
        </Button>
      </header>
      <main className="flex-1 p-8">
        <Outlet />
      </main>
    </div>
  );
}

function DashboardPage() {
  const { me } = useAuth();
  return (
    <div>
      <h1 className="text-xl font-semibold">Dashboard — coming soon</h1>
      {me && (
        <p className="mt-2 text-sm text-muted-foreground">
          Logged in as {me.user.name} ({me.user.email}) — {me.business.name}
        </p>
      )}
    </div>
  );
}

// Exported separately from the built router (see router.tsx) so tests can
// render these routes with a MemoryRouter-backed router instead of depending
// on jsdom's window.location.
export const routes: RouteObject[] = [
  { path: "/", element: <Navigate to="/login" replace /> },
  { path: "/login", element: <LoginPage /> },
  { path: "/signup", element: <SignupPage /> },
  { path: "/password-reset", element: <PasswordResetRequestPage /> },
  { path: "/password-reset/confirm", element: <PasswordResetConfirmPage /> },
  {
    path: "/app",
    element: <ProtectedRoute />,
    children: [
      {
        path: "",
        element: <AppLayout />,
        children: [
          { index: true, element: <Navigate to="/app/dashboard" replace /> },
          { path: "dashboard", element: <DashboardPage /> },
          { path: "account", element: <AccountSettingsPage /> },
          { path: "customers", element: <CustomerListPage /> },
          { path: "customers/new", element: <CustomerFormPage /> },
          { path: "customers/:id", element: <CustomerDetailPage /> },
          { path: "customers/:id/edit", element: <CustomerFormPage /> },
          { path: "products", element: <ProductListPage /> },
          { path: "products/new", element: <ProductFormPage /> },
          { path: "products/:id", element: <ProductDetailPage /> },
          { path: "products/:id/edit", element: <ProductFormPage /> },
          { path: "products/:id/recipe/new", element: <RecipeCreatePage /> },
          { path: "products/:id/recipe/edit", element: <RecipeEditPage /> },
          { path: "products/:id/recipe/history", element: <RecipeHistoryPage /> },
          {
            path: "products/:id/recipe/revisions/:revisionId",
            element: <RecipeRevisionDetailPage />,
          },
          { path: "inventory/ingredients", element: <IngredientListPage /> },
          { path: "inventory/ingredients/new", element: <IngredientFormPage /> },
          { path: "inventory/ingredients/:id", element: <IngredientDetailPage /> },
          { path: "inventory/ingredients/:id/edit", element: <IngredientFormPage /> },
        ],
      },
    ],
  },
];
