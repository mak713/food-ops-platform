// Purchased Product Inventory Initial Balance (Phase 5 Plan §E) — creates the inventory
// row for a PURCHASED Product that has never had any inventory activity yet.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { productsApi } from "../products/api";
import { PurchasedPurchaseForm, type PurchasedPurchaseInput } from "./PurchasedPurchaseForm";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

export function PurchasedInitialBalancePage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["products", productId],
    queryFn: () => productsApi.get(productId),
    retry: false,
  });

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["products", productId, "purchased-inventory"] }),
      queryClient.invalidateQueries({ queryKey: ["purchased-inventory"] }),
    ]);

  const mutation = useMutation({
    mutationFn: (input: PurchasedPurchaseInput) =>
      purchasedInventoryApi.createInitialBalance(productId, input),
    onSuccess: async () => {
      await invalidate();
      navigate(`/app/products/${productId}`);
    },
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError) {
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }
  const product = query.data;
  if (!product) {
    return null;
  }

  if (!product.is_active) {
    return (
      <div className="mx-auto flex max-w-lg flex-col gap-3">
        <h1 className="text-xl font-semibold">Initial Balance — {product.name}</h1>
        <p className="text-sm text-muted-foreground">
          This product is inactive. An initial balance cannot be recorded while
          inactive — reactivate it first from its detail page, then return here.
        </p>
        <Button variant="outline" onClick={() => navigate(`/app/products/${productId}`)}>
          Back to product
        </Button>
      </div>
    );
  }

  const isConflict =
    mutation.error instanceof ApiError &&
    (mutation.error.body?.error.code === "PURCHASED_INVENTORY_ALREADY_INITIALIZED" ||
      mutation.error.body?.error.code === "PURCHASED_INVENTORY_PRODUCT_INACTIVE" ||
      mutation.error.body?.error.code === "PURCHASED_INVENTORY_REQUIRES_PURCHASED_PRODUCT");

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Initial Balance — {product.name}</h1>
      <p className="text-sm text-muted-foreground">
        Record the starting stock and cost basis for this product. This can only be done
        once — later changes use Restock or Manual Adjustment.
      </p>

      <PurchasedPurchaseForm
        isPending={mutation.isPending}
        mutationError={isConflict ? null : mutation.error}
        submitLabel="Save initial balance"
        onSubmit={(input) => mutation.mutate(input)}
        onCancel={() => navigate(`/app/products/${productId}`)}
      />

      {isConflict && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p role="alert">
            {mutation.error instanceof ApiError
              ? mutation.error.body?.error.message
              : "This product's inventory can no longer be initialized here."}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => navigate(`/app/products/${productId}`)}
          >
            Back to product
          </Button>
        </div>
      )}
    </div>
  );
}
