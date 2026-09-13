// Ingredient inventory transaction history (Phase 5 Plan §D/§F) — readable regardless of
// the Ingredient's active state (approval decision 6).

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { InventoryTransactionTable } from "../../components/shared/InventoryTransactionTable";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { ingredientsApi } from "./api";
import { ingredientInventoryApi } from "./inventoryApi";

export function IngredientInventoryHistoryPage() {
  const { id } = useParams<{ id: string }>();
  const ingredientId = id as string;
  const navigate = useNavigate();

  const query = useQuery({
    queryKey: ["ingredients", ingredientId],
    queryFn: () => ingredientsApi.get(ingredientId),
    retry: false,
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError) {
    const isNotFound = query.error instanceof ApiError && query.error.status === 404;
    if (isNotFound) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">Ingredient not found</h1>
          <Button variant="outline" onClick={() => navigate("/app/inventory/ingredients")}>
            Back to ingredients
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }
  const ingredient = query.data;
  if (!ingredient) {
    return null;
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Inventory History — {ingredient.name}</h1>
        <Link
          to={`/app/inventory/ingredients/${ingredientId}`}
          className="text-sm text-muted-foreground underline-offset-4 hover:underline"
        >
          Back to ingredient
        </Link>
      </div>

      <InventoryTransactionTable
        queryKey={["ingredients", ingredientId, "inventory", "transactions"]}
        fetchPage={(params) => ingredientInventoryApi.listTransactions(ingredientId, params)}
      />
    </div>
  );
}
