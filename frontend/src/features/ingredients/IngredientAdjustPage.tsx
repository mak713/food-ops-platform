// Ingredient Manual Adjustment (Phase 5 Plan §D) — allowed even on an archived ingredient
// (approval decision 6), and may legitimately drive physical quantity negative (decision 2).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { ingredientsApi } from "./api";
import { IngredientAdjustmentForm, type IngredientAdjustmentInput } from "./IngredientAdjustmentForm";
import { ingredientInventoryApi } from "./inventoryApi";

export function IngredientAdjustPage() {
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
    mutationFn: (input: IngredientAdjustmentInput) =>
      ingredientInventoryApi.adjust(ingredientId, { version: query.data!.version, ...input }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["ingredients", ingredientId] }),
        queryClient.invalidateQueries({ queryKey: ["ingredients"] }),
      ]);
      navigate(`/app/inventory/ingredients/${ingredientId}`);
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

  const isStale =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Manual Adjustment — {ingredient.name}</h1>

      <IngredientAdjustmentForm
        canonicalUnit={ingredient.canonical_unit}
        isPending={mutation.isPending}
        mutationError={isStale ? null : mutation.error}
        onSubmit={(input) => mutation.mutate(input)}
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
