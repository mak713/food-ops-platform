// Ingredient Replacement Cost maintenance page (Phase 5 Plan §D, correction-pass finding
// 1) — blocked for an archived Ingredient (backend-enforced 409; this page shows an
// explanation and a path back rather than a misleading usable form when navigated to
// directly, per finding 3).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { ingredientsApi } from "./api";
import { IngredientReplacementCostForm } from "./IngredientReplacementCostForm";
import { ingredientInventoryApi } from "./inventoryApi";

export function IngredientReplacementCostPage() {
  const { id } = useParams<{ id: string }>();
  const ingredientId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["ingredients", ingredientId],
    queryFn: () => ingredientsApi.get(ingredientId),
    retry: false,
  });

  const mutation = useMutation({
    mutationFn: (replacementUnitCost: string | null) =>
      ingredientInventoryApi.setReplacementCost(ingredientId, {
        version: query.data!.version,
        replacement_unit_cost: replacementUnitCost,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["ingredients", ingredientId] }),
        queryClient.invalidateQueries({ queryKey: ["ingredients"] }),
      ]);
    },
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError) {
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }
  const ingredient = query.data;
  if (!ingredient) {
    return null;
  }

  if (!ingredient.is_active) {
    return (
      <div className="mx-auto flex max-w-lg flex-col gap-3">
        <h1 className="text-xl font-semibold">Replacement Cost — {ingredient.name}</h1>
        <p className="text-sm text-muted-foreground">
          This ingredient is archived. Replacement-cost maintenance is unavailable while
          archived — reactivate it first from its detail page, then return here.
        </p>
        <Button
          variant="outline"
          onClick={() => navigate(`/app/inventory/ingredients/${ingredientId}`)}
        >
          Back to ingredient
        </Button>
      </div>
    );
  }

  const isStale =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Replacement Cost — {ingredient.name}</h1>
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate(`/app/inventory/ingredients/${ingredientId}`)}
        >
          Back to ingredient
        </Button>
      </div>

      <IngredientReplacementCostForm
        canonicalUnit={ingredient.canonical_unit}
        latestPurchaseUnitCost={ingredient.latest_purchase_unit_cost}
        replacementUnitCost={ingredient.replacement_unit_cost}
        effectiveReplacementCost={ingredient.effective_replacement_cost}
        isPending={mutation.isPending}
        mutationError={isStale ? null : mutation.error}
        onSetOverride={(value) => mutation.mutate(value)}
        onClearOverride={() => mutation.mutate(null)}
        onCancel={() => navigate(`/app/inventory/ingredients/${ingredientId}`)}
      />

      {isStale && (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p role="alert">
            This ingredient changed since you opened this page. Refresh and try again.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={async () => {
              mutation.reset();
              await query.refetch();
            }}
          >
            Refresh
          </Button>
        </div>
      )}
    </div>
  );
}
