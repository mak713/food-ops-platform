// Purchased Product Inventory Restock (Phase 5 Plan §E) — may itself be the first-ever
// inventory event for this Product (approval decision 4): if no inventory row exists yet,
// the request omits `version` entirely rather than treating "not found" as an error.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { productsApi } from "../products/api";
import { PurchasedPurchaseForm, type PurchasedPurchaseInput } from "./PurchasedPurchaseForm";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

export function PurchasedRestockPage() {
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

  const isNotYetInitialized =
    inventoryQuery.isError &&
    inventoryQuery.error instanceof ApiError &&
    inventoryQuery.error.status === 404;

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["products", productId, "purchased-inventory"] }),
      queryClient.invalidateQueries({ queryKey: ["purchased-inventory"] }),
    ]);

  const mutation = useMutation({
    mutationFn: (input: PurchasedPurchaseInput) =>
      purchasedInventoryApi.restock(productId, {
        version: inventoryQuery.data?.version,
        ...input,
      }),
    onSuccess: async () => {
      await invalidate();
      navigate(`/app/products/${productId}`);
    },
  });

  if (productQuery.isLoading || inventoryQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (productQuery.isError) {
    return <ErrorBanner error={productQuery.error} onRetry={() => productQuery.refetch()} />;
  }
  if (inventoryQuery.isError && !isNotYetInitialized) {
    return <ErrorBanner error={inventoryQuery.error} onRetry={() => inventoryQuery.refetch()} />;
  }
  const product = productQuery.data;
  if (!product) {
    return null;
  }

  if (!product.is_active) {
    return (
      <div className="mx-auto flex max-w-lg flex-col gap-3">
        <h1 className="text-xl font-semibold">Restock — {product.name}</h1>
        <p className="text-sm text-muted-foreground">
          This product is inactive. Restocking is unavailable while inactive —
          reactivate it first from its detail page, then return here.
        </p>
        <Button variant="outline" onClick={() => navigate(`/app/products/${productId}`)}>
          Back to product
        </Button>
      </div>
    );
  }

  const isStateChanged =
    mutation.error instanceof ApiError &&
    mutation.error.body?.error.code === "PURCHASED_INVENTORY_STATE_CHANGED";
  const isOtherError = mutation.error && !isStateChanged;

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Restock — {product.name}</h1>
      {isNotYetInitialized && (
        <p className="text-sm text-muted-foreground">
          This product has no recorded inventory yet — this restock will be its first.
        </p>
      )}

      <PurchasedPurchaseForm
        isPending={mutation.isPending}
        mutationError={isOtherError ? mutation.error : null}
        submitLabel="Save restock"
        onSubmit={(input) => mutation.mutate(input)}
        onCancel={() => navigate(`/app/products/${productId}`)}
      />

      {isStateChanged && (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p role="alert">
            {mutation.error instanceof ApiError
              ? mutation.error.body?.error.message
              : "This product's inventory changed since you opened this page. Refresh and try again."}
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
