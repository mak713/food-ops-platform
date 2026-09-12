// Recipe editing = creating a new revision (Phase 4 Plan v4 §5/§6a/§13). Saving never
// mutates the current revision in place — it submits the complete new content, which
// becomes revision N+1.
//
// `expectedRevisionId` is captured once, when this edit session begins (on first
// successful load, or after an explicit Refresh) — never silently re-derived from a live
// query value while the editor stays mounted, mirroring the exact fix already shipped for
// SellingOptionRow's `editSessionVersion`. A background refetch of the recipe query (e.g.
// triggered by some unrelated action) must not quietly advance what this edit session
// believes its base revision is.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { ingredientsApi } from "../ingredients/api";
import { RecipeContentForm, type IngredientOption } from "./RecipeContentForm";
import { recipeApi, type RecipeContentInput, type RecipeRevision } from "./recipeApi";

interface EditSession {
  expectedRevisionId: string;
  formKey: number;
}

function toFormValues(revision: RecipeRevision) {
  return {
    yield_quantity: revision.yield_quantity,
    active_time_minutes: String(revision.active_time_minutes),
    elapsed_time_minutes:
      revision.elapsed_time_minutes != null ? String(revision.elapsed_time_minutes) : "",
    notes: revision.notes ?? "",
    ingredients: revision.ingredients.map((line) => ({
      ingredient_id: line.ingredient_id,
      quantity: line.quantity,
      unit: line.unit,
    })),
  };
}

export function RecipeEditPage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const recipeQuery = useQuery({
    queryKey: ["products", productId, "recipe"],
    queryFn: () => recipeApi.get(productId),
    retry: false,
  });
  const ingredientsQuery = useQuery({
    queryKey: ["ingredients", "all-active"],
    queryFn: () => ingredientsApi.listAllActive(),
  });

  const [session, setSession] = useState<EditSession | null>(null);

  // Captured once, on first render after the recipe loads — not in an effect, since this
  // is a one-time render-time state adjustment (React's documented pattern for it), not a
  // synchronization with an external system. `session === null` only guards the *first*
  // capture; it deliberately does not re-fire on a later background refetch (see file
  // header) — the "Refresh" flow below re-captures explicitly via its own setSession call.
  if (recipeQuery.data && session === null) {
    setSession({ expectedRevisionId: recipeQuery.data.current_revision.id, formKey: 0 });
  }

  const mutation = useMutation({
    mutationFn: (content: RecipeContentInput) =>
      recipeApi.createRevision(productId, {
        expected_current_revision_id: session!.expectedRevisionId,
        ...content,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["products", productId] });
      navigate(`/app/products/${productId}`);
    },
  });

  const isConflict =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "RECIPE_REVISION_CONFLICT";

  const handleRefreshAfterConflict = async () => {
    mutation.reset();
    const fresh = await recipeQuery.refetch();
    if (fresh.data) {
      setSession((prev) => ({
        expectedRevisionId: fresh.data.current_revision.id,
        formKey: (prev?.formKey ?? 0) + 1,
      }));
    }
  };

  if (recipeQuery.isLoading || ingredientsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (recipeQuery.isError) {
    const isNotFound = recipeQuery.error instanceof ApiError && recipeQuery.error.status === 404;
    if (isNotFound) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">No recipe yet</h1>
          <Button variant="outline" onClick={() => navigate(`/app/products/${productId}`)}>
            Back to product
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={recipeQuery.error} onRetry={() => recipeQuery.refetch()} />;
  }

  if (ingredientsQuery.isError) {
    return <ErrorBanner error={ingredientsQuery.error} onRetry={() => ingredientsQuery.refetch()} />;
  }

  if (!recipeQuery.data || !session) {
    return null;
  }

  const activeIngredients: IngredientOption[] = (ingredientsQuery.data ?? []).map((i) => ({
    id: i.id,
    name: i.name,
    measurement_family: i.measurement_family,
    is_active: i.is_active,
  }));
  // Carried-forward archived Ingredients (present on the current revision's own lines but
  // no longer active) still need a lookup entry — for display ("Archived" label) and so
  // their existing unit stays selectable (family derived from that unit, not from here).
  const archivedCarriedForward: IngredientOption[] = recipeQuery.data.current_revision.ingredients
    .filter((line) => !line.ingredient_is_active)
    .map((line) => ({ id: line.ingredient_id, name: line.ingredient_name, is_active: false }));
  const ingredientLookup = Object.fromEntries(
    [...activeIngredients, ...archivedCarriedForward].map((i) => [i.id, i]),
  );

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Edit Recipe — {recipeQuery.data.name}</h1>

      <RecipeContentForm
        key={session.formKey}
        defaultValues={toFormValues(recipeQuery.data.current_revision)}
        activeIngredients={activeIngredients}
        ingredientLookup={ingredientLookup}
        isPending={mutation.isPending}
        mutationError={isConflict ? null : mutation.error}
        submitLabel="Save as new revision"
        headerNote={
          <p className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground">
            Saving creates a new recipe revision — the current revision (#
            {recipeQuery.data.current_revision.revision_number}) stays unchanged and
            remains viewable in history.
          </p>
        }
        onSubmit={(content) => mutation.mutate(content)}
        onCancel={() => navigate(`/app/products/${productId}`)}
      />

      {isConflict && (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p role="alert">
            This recipe changed since you started editing. Refresh the latest revision and
            review your changes before saving again.
          </p>
          <Button type="button" variant="outline" size="sm" onClick={handleRefreshAfterConflict}>
            Refresh
          </Button>
        </div>
      )}
    </div>
  );
}
