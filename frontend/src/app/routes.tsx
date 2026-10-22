import { useMutation } from "@tanstack/react-query";
import type { RouteObject } from "react-router-dom";
import { Link, Navigate, Outlet, useNavigate } from "react-router-dom";
import { Button } from "../components/ui/button";
import { authApi } from "../features/auth/api";
import { useAuth } from "../features/auth/AuthContext";
import { AnalyticsPage } from "../features/analytics/AnalyticsPage";
import { LoginPage } from "../features/auth/LoginPage";
import { PasswordResetConfirmPage } from "../features/auth/PasswordResetConfirmPage";
import { PasswordResetRequestPage } from "../features/auth/PasswordResetRequestPage";
import { ProtectedRoute } from "../features/auth/ProtectedRoute";
import { SettingsPage } from "../features/settings/SettingsPage";
import { SignupPage } from "../features/auth/SignupPage";
import { CustomerDetailPage } from "../features/customers/CustomerDetailPage";
import { CustomerFormPage } from "../features/customers/CustomerFormPage";
import { CustomerListPage } from "../features/customers/CustomerListPage";
import { DashboardPage } from "../features/dashboard/DashboardPage";
import { IngredientAdjustPage } from "../features/ingredients/IngredientAdjustPage";
import { IngredientDetailPage } from "../features/ingredients/IngredientDetailPage";
import { IngredientFormPage } from "../features/ingredients/IngredientFormPage";
import { IngredientInitialBalancePage } from "../features/ingredients/IngredientInitialBalancePage";
import { IngredientInventoryHistoryPage } from "../features/ingredients/IngredientInventoryHistoryPage";
import { IngredientListPage } from "../features/ingredients/IngredientListPage";
import { IngredientReplacementCostPage } from "../features/ingredients/IngredientReplacementCostPage";
import { IngredientRestockPage } from "../features/ingredients/IngredientRestockPage";
import { InventoryHistoryPage } from "../features/inventory/InventoryHistoryPage";
import { OrderDetailPage } from "../features/orders/OrderDetailPage";
import { OrderEntryPage } from "../features/orders/OrderEntryPage";
import { OrderListPage } from "../features/orders/OrderListPage";
import { InventoryHubPage } from "../features/inventory/InventoryHubPage";
import { PurchasedAdjustPage } from "../features/inventory/PurchasedAdjustPage";
import { PurchasedInitialBalancePage } from "../features/inventory/PurchasedInitialBalancePage";
import { PurchasedInventoryHistoryPage } from "../features/inventory/PurchasedInventoryHistoryPage";
import { PurchasedProductInventoryListPage } from "../features/inventory/PurchasedProductInventoryListPage";
import { PurchasedReplacementCostPage } from "../features/inventory/PurchasedReplacementCostPage";
import { PurchasedRestockPage } from "../features/inventory/PurchasedRestockPage";
import { ProductDetailPage } from "../features/products/ProductDetailPage";
import { ProductFormPage } from "../features/products/ProductFormPage";
import { ProductListPage } from "../features/products/ProductListPage";
import { RecipeCreatePage } from "../features/products/RecipeCreatePage";
import { RecipeEditPage } from "../features/products/RecipeEditPage";
import { RecipeHistoryPage } from "../features/products/RecipeHistoryPage";
import { RecipeRevisionDetailPage } from "../features/products/RecipeRevisionDetailPage";
import { ProductionPlanPage } from "../features/production/ProductionPlanPage";
import { ProductionRunDetailPage } from "../features/production/ProductionRunDetailPage";
import { ProductionRunListPage } from "../features/production/ProductionRunListPage";
import { ShoppingListPage } from "../features/production/ShoppingListPage";
import { StartProductionPage } from "../features/production/StartProductionPage";
import { SurplusAdjustPage } from "../features/surplus/SurplusAdjustPage";
import { SurplusCreatePage } from "../features/surplus/SurplusCreatePage";
import { SurplusHistoryPage } from "../features/surplus/SurplusHistoryPage";
import { SurplusListPage } from "../features/surplus/SurplusListPage";

// Phase 2 establishes the real authenticated app shell (Spec §12.2): every route under
// /app is gated by ProtectedRoute, which derives auth state from the server-managed
// session (GET /api/v1/auth/me) — never from anything client-stored. Every other module
// under §12.2 (Orders, Production, Customers, ...) is added when the phase that owns it
// begins. Phase 9 replaces the Dashboard placeholder with the real Dashboard and adds the
// Shopping List under Production. Phase 10 adds Analytics. Phase 11 replaces the standalone
// Account page with Settings (`/app/account` redirects to `/app/settings?area=account`).

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
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border p-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
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
          <Link to="/app/orders" className="text-muted-foreground hover:text-foreground">
            Orders
          </Link>
          <Link to="/app/inventory" className="text-muted-foreground hover:text-foreground">
            Inventory
          </Link>
          <Link to="/app/production" className="text-muted-foreground hover:text-foreground">
            Production
          </Link>
          <Link
            to="/app/production/shopping-list"
            className="text-muted-foreground hover:text-foreground"
          >
            Shopping List
          </Link>
          <Link to="/app/surplus" className="text-muted-foreground hover:text-foreground">
            Surplus
          </Link>
          <Link to="/app/analytics" className="text-muted-foreground hover:text-foreground">
            Analytics
          </Link>
          <Link to="/app/settings" className="text-muted-foreground hover:text-foreground">
            Settings
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
          { path: "analytics", element: <AnalyticsPage /> },
          { path: "settings", element: <SettingsPage /> },
          // Compatibility: the former standalone Account page now lives in Settings.
          { path: "account", element: <Navigate to="/app/settings?area=account" replace /> },
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
          { path: "inventory", element: <InventoryHubPage /> },
          { path: "inventory/ingredients", element: <IngredientListPage /> },
          { path: "inventory/ingredients/new", element: <IngredientFormPage /> },
          { path: "inventory/ingredients/:id", element: <IngredientDetailPage /> },
          { path: "inventory/ingredients/:id/edit", element: <IngredientFormPage /> },
          {
            path: "inventory/ingredients/:id/initial-balance",
            element: <IngredientInitialBalancePage />,
          },
          { path: "inventory/ingredients/:id/restock", element: <IngredientRestockPage /> },
          { path: "inventory/ingredients/:id/adjust", element: <IngredientAdjustPage /> },
          {
            path: "inventory/ingredients/:id/replacement-cost",
            element: <IngredientReplacementCostPage />,
          },
          {
            path: "inventory/ingredients/:id/history",
            element: <IngredientInventoryHistoryPage />,
          },
          {
            path: "inventory/purchased-products",
            element: <PurchasedProductInventoryListPage />,
          },
          {
            path: "inventory/purchased-products/:id/initial-balance",
            element: <PurchasedInitialBalancePage />,
          },
          {
            path: "inventory/purchased-products/:id/restock",
            element: <PurchasedRestockPage />,
          },
          {
            path: "inventory/purchased-products/:id/adjust",
            element: <PurchasedAdjustPage />,
          },
          {
            path: "inventory/purchased-products/:id/replacement-cost",
            element: <PurchasedReplacementCostPage />,
          },
          {
            path: "inventory/purchased-products/:id/history",
            element: <PurchasedInventoryHistoryPage />,
          },
          { path: "inventory/history", element: <InventoryHistoryPage /> },
          { path: "orders", element: <OrderListPage /> },
          { path: "orders/new", element: <OrderEntryPage /> },
          { path: "orders/:id", element: <OrderDetailPage /> },
          { path: "orders/:id/edit", element: <OrderEntryPage /> },
          { path: "production", element: <ProductionPlanPage /> },
          { path: "production/start/:requirementId", element: <StartProductionPage /> },
          { path: "production/shopping-list", element: <ShoppingListPage /> },
          { path: "production/runs", element: <ProductionRunListPage /> },
          { path: "production/runs/:id", element: <ProductionRunDetailPage /> },
          { path: "surplus", element: <SurplusListPage /> },
          { path: "surplus/new", element: <SurplusCreatePage /> },
          { path: "surplus/:id/adjust", element: <SurplusAdjustPage /> },
          { path: "surplus/:id/history", element: <SurplusHistoryPage /> },
        ],
      },
    ],
  },
];
