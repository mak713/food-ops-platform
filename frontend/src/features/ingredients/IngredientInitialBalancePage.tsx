// Ingredient Initial Balance (Phase 5 Plan §D) — one-time inventory-initialization entry
// point; the backend rejects a second attempt (409 already-initialized) regardless of
// what this page shows, but the page only offers itself as a link from the detail page
// when the Ingredient looks never-touched (§F).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { ingredientsApi } from "./api";
import { IngredientPurchaseForm, type IngredientPurchaseInput } from "./IngredientPurchaseForm";
import { ingredientInventoryApi } from "./inventoryApi";

export function IngredientInitialBalancePage() {
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
    mutationFn: (input: IngredientPurchaseInput) =>
      ingredientInventoryApi.createInitialBalance(ingredientId, {
        version: query.data!.version,
        ...input,
      }),
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

  if (!ingredient.is_active) {
    return (
      <div className="mx-auto flex max-w-lg flex-col gap-3">
        <h1 className="text-xl font-semibold">Initial Balance — {ingredient.name}</h1>
        <p className="text-sm text-muted-foreground">
          This ingredient is archived. An initial balance cannot be recorded while
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

  const isConflict =
    mutation.error instanceof ApiError &&
    (mutation.error.body?.error.code === "INGREDIENT_INVENTORY_ALREADY_INITIALIZED" ||
      mutation.error.body?.error.code === "STALE_VERSION");

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Initial Balance — {ingredient.name}</h1>
      <p className="text-sm text-muted-foreground">
        Record the starting stock and cost basis for this ingredient. This can only be
        done once — later changes use Restock or Manual Adjustment.
      </p>

      <IngredientPurchaseForm
        measurementFamily={ingredient.measurement_family}
        defaultValues={{
          quantity: "",
          unit: ingredient.canonical_unit,
          unit_cost: "",
          supplier_text: "",
          notes: "",
        }}
        isPending={mutation.isPending}
        mutationError={isConflict ? null : mutation.error}
        submitLabel="Save initial balance"
        onSubmit={(input) => mutation.mutate(input)}
        onCancel={() => navigate(`/app/inventory/ingredients/${ingredientId}`)}
      />

      {isConflict && (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p role="alert">
            {mutation.error instanceof ApiError
              ? mutation.error.body?.error.message
              : "This ingredient's inventory can no longer be initialized here."}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={() => navigate(`/app/inventory/ingredients/${ingredientId}`)}
          >
            Back to ingredient
          </Button>
        </div>
      )}
    </div>
  );
}
