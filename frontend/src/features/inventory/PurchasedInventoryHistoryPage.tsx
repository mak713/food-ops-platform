// Purchased Product Inventory transaction history (Phase 5 Plan §E/§F) — readable
// regardless of the Product's active state (approval decision 6).

import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { InventoryTransactionTable } from "../../components/shared/InventoryTransactionTable";
import { productsApi } from "../products/api";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

export function PurchasedInventoryHistoryPage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;

  const query = useQuery({
    queryKey: ["products", productId],
    queryFn: () => productsApi.get(productId),
    retry: false,
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

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Inventory History — {product.name}</h1>
        <Link
          to={`/app/products/${productId}`}
          className="text-sm text-muted-foreground underline-offset-4 hover:underline"
        >
          Back to product
        </Link>
      </div>

      <InventoryTransactionTable
        queryKey={["products", productId, "purchased-inventory", "transactions"]}
        fetchPage={(params) => purchasedInventoryApi.listTransactions(productId, params)}
      />
    </div>
  );
}
