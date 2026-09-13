// Purchased Product Inventory Manual Adjustment (Phase 5 Plan §E) — requires the
// inventory row to already exist (unlike Restock, Adjustment never creates it); allowed
// even on an inactive Product (approval decision 6).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { productsApi } from "../products/api";
import { PurchasedAdjustmentForm, type PurchasedAdjustmentInput } from "./PurchasedAdjustmentForm";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

export function PurchasedAdjustPage() {
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
    mutationFn: (input: PurchasedAdjustmentInput) =>
      purchasedInventoryApi.adjust(productId, { version: inventoryQuery.data!.version, ...input }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["products", productId, "purchased-inventory"] }),
        queryClient.invalidateQueries({ queryKey: ["purchased-inventory"] }),
      ]);
      navigate(`/app/products/${productId}`);
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

  if (inventoryQuery.isError) {
    const isNotInitialized =
      inventoryQuery.error instanceof ApiError &&
      inventoryQuery.error.body?.error.code === "PURCHASED_INVENTORY_NOT_INITIALIZED";
    if (isNotInitialized) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">No inventory recorded yet</h1>
          <p className="text-sm text-muted-foreground">
            Record an Initial Balance or Restock before adjusting {product.name}.
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
      <h1 className="text-xl font-semibold">Manual Adjustment — {product.name}</h1>

      <PurchasedAdjustmentForm
        isPending={mutation.isPending}
        mutationError={isStale ? null : mutation.error}
        onSubmit={(input) => mutation.mutate(input)}
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
