// Purchased Product Inventory Replacement Cost maintenance page (Phase 5 Plan §E,
// correction-pass finding 1) — blocked for an inactive Product (backend-enforced 409)
// and requires an already-initialized inventory row (backend-enforced 404); both show an
// explanation and a path back rather than a misleading usable form (finding 3).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { productsApi } from "../products/api";
import { PurchasedReplacementCostForm } from "./PurchasedReplacementCostForm";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

export function PurchasedReplacementCostPage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const productQuery = useQuery({
    queryKey: ["products", productId],
    queryFn: () => productsApi.get(productId),
    retry: false,
  });

  const inventoryQuery = useQuery({
    queryKey: ["products", productId, "purchased-inventory"],
    queryFn: () => purchasedInventoryApi.get(productId),
    retry: false,
  });

  const mutation = useMutation({
    mutationFn: (replacementUnitCost: string | null) =>
      purchasedInventoryApi.setReplacementCost(productId, {
        version: inventoryQuery.data!.version,
        replacement_unit_cost: replacementUnitCost,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["products", productId, "purchased-inventory"] }),
        queryClient.invalidateQueries({ queryKey: ["purchased-inventory"] }),
      ]);
    },
  });

  if (productQuery.isLoading || inventoryQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (productQuery.isError) {
    return <ErrorBanner error={productQuery.error} onRetry={() => productQuery.refetch()} />;
  }
  const product = productQuery.data;
  if (!product) {
    return null;
  }

  if (!product.is_active) {
    return (
      <div className="mx-auto flex max-w-lg flex-col gap-3">
        <h1 className="text-xl font-semibold">Replacement Cost — {product.name}</h1>
        <p className="text-sm text-muted-foreground">
          This product is inactive. Replacement-cost maintenance is unavailable while
          inactive — reactivate it first from its detail page, then return here.
        </p>
        <Button variant="outline" onClick={() => navigate(`/app/products/${productId}`)}>
          Back to product
        </Button>
      </div>
    );
  }

  if (inventoryQuery.isError) {
    const isNotInitialized =
      inventoryQuery.error instanceof ApiError &&
      inventoryQuery.error.body?.error.code === "PURCHASED_INVENTORY_NOT_INITIALIZED";
    if (isNotInitialized) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">No inventory recorded yet</h1>
          <p className="text-sm text-muted-foreground">
            Record an Initial Balance or Restock before maintaining a replacement cost for
            {" "}
            {product.name}.
          </p>
          <Button variant="outline" onClick={() => navigate(`/app/products/${productId}`)}>
            Back to product
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={inventoryQuery.error} onRetry={() => inventoryQuery.refetch()} />;
  }

  const isStale =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Replacement Cost — {product.name}</h1>
        <Button variant="outline" size="sm" onClick={() => navigate(`/app/products/${productId}`)}>
          Back to product
        </Button>
      </div>

      <PurchasedReplacementCostForm
        latestPurchaseUnitCost={inventoryQuery.data?.latest_purchase_unit_cost ?? null}
        replacementUnitCost={inventoryQuery.data?.replacement_unit_cost ?? null}
        effectiveReplacementCost={inventoryQuery.data?.effective_replacement_cost ?? null}
        isPending={mutation.isPending}
        mutationError={isStale ? null : mutation.error}
        onSetOverride={(value) => mutation.mutate(value)}
        onClearOverride={() => mutation.mutate(null)}
        onCancel={() => navigate(`/app/products/${productId}`)}
      />

      {isStale && (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p role="alert">
            This product's inventory changed since you opened this page. Refresh and try
            again.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={async () => {
              mutation.reset();
              await inventoryQuery.refetch();
            }}
          >
            Refresh
          </Button>
        </div>
      )}
    </div>
  );
}
