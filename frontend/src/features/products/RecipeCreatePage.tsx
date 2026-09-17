// Recipe creation (Phase 4 Plan v4 §5/§13) — Recipe + Revision 1 + all ingredient lines
// are created atomically in one request; there is no separate "create empty recipe" step.
//
// Phase 7 Implementation Remediation Plan, Finding 3: the initial submit omits
// `apply_scope`. If the Product already has confirmed `INCOMPLETE_RECIPE` unstarted
// demand, the backend responds with the structured `RECIPE_REVISION_IMPACT_REQUIRED`
// (422) — this page opens the same "Apply Existing" / "Future Only" dialog
// `RecipeEditPage` uses and resubmits with the chosen scope. Unlike a replacement
// revision, first-Recipe creation has no `expected_current_revision_id` at all (Final
// Pre-Implementation Amendment §5) — a concurrent-creation race is instead surfaced as
// the existing `RECIPE_ALREADY_EXISTS` 409, not a new concurrency code.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError, type ApiErrorIssue } from "../../api/client";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { ingredientsApi } from "../ingredients/api";
import { productsApi } from "./api";
import { recipeApi, type ApplyScope, type RecipeContentInput } from "./recipeApi";
import { RecipeContentForm, type IngredientOption } from "./RecipeContentForm";
import { useState } from "react";

interface AffectedOrderLine {
  order_id: string;
  order_line_id: string;
  product_id: string;
  demand_date: string;
}

export function RecipeCreatePage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [pendingContent, setPendingContent] = useState<RecipeContentInput | null>(null);

  const productQuery = useQuery({
    queryKey: ["products", productId],
    queryFn: () => productsApi.get(productId),
    retry: false,
  });
  const ingredientsQuery = useQuery({
    queryKey: ["ingredients", "all-active"],
    queryFn: () => ingredientsApi.listAllActive(),
  });

  const mutation = useMutation({
    mutationFn: ({
      content,
      applyScope,
    }: {
      content: RecipeContentInput;
      applyScope?: ApplyScope;
    }) => recipeApi.create(productId, { name, apply_scope: applyScope, ...content }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["products", productId] });
      navigate(`/app/products/${productId}`);
    },
  });

  const impactRequiredIssue: ApiErrorIssue | undefined =
    mutation.error instanceof ApiError &&
    mutation.error.body?.error.code === "RECIPE_REVISION_IMPACT_REQUIRED"
      ? mutation.error.body.error.issues[0]
      : undefined;
  const affectedOrderLines = (impactRequiredIssue?.details.affected_order_lines ??
    []) as AffectedOrderLine[];

  const handleChooseScope = (applyScope: ApplyScope) => {
    if (!pendingContent) return;
    mutation.mutate({ content: pendingContent, applyScope });
  };

  if (productQuery.isLoading || ingredientsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (productQuery.isError) {
    const isNotFound = productQuery.error instanceof ApiError && productQuery.error.status === 404;
    if (isNotFound) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">Product not found</h1>
          <Button variant="outline" onClick={() => navigate("/app/products")}>
            Back to products
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={productQuery.error} onRetry={() => productQuery.refetch()} />;
  }

  if (ingredientsQuery.isError) {
    return <ErrorBanner error={ingredientsQuery.error} onRetry={() => ingredientsQuery.refetch()} />;
  }

  const activeIngredients: IngredientOption[] = (ingredientsQuery.data ?? []).map((i) => ({
    id: i.id,
    name: i.name,
    measurement_family: i.measurement_family,
    is_active: i.is_active,
  }));
  const ingredientLookup = Object.fromEntries(activeIngredients.map((i) => [i.id, i]));

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Create Recipe — {productQuery.data?.name}</h1>

      <div className="flex flex-col gap-1">
        <label htmlFor="recipe-name" className="text-sm font-medium">
          Recipe name
        </label>
        <Input
          id="recipe-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setNameError(null);
          }}
        />
        {nameError && <p className="text-xs text-destructive">{nameError}</p>}
      </div>

      <RecipeContentForm
        defaultValues={{
          yield_quantity: "",
          active_time_minutes: "",
          elapsed_time_minutes: "",
          notes: "",
          ingredients: [],
        }}
        activeIngredients={activeIngredients}
        ingredientLookup={ingredientLookup}
        isPending={mutation.isPending}
        mutationError={impactRequiredIssue ? null : mutation.error}
        submitLabel="Create Recipe"
        onSubmit={(content) => {
          if (!name.trim()) {
            setNameError("Recipe name is required");
            return;
          }
          setPendingContent(content);
          mutation.mutate({ content });
        }}
        onCancel={() => navigate(`/app/products/${productId}`)}
      />

      <AlertDialog open={Boolean(impactRequiredIssue)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>This recipe affects confirmed orders</AlertDialogTitle>
            <AlertDialogDescription>
              {affectedOrderLines.length} confirmed order line
              {affectedOrderLines.length === 1 ? "" : "s"} for this product currently have
              no recipe. Choose how this first recipe should apply:
              <br />
              <strong>Apply Existing</strong> migrates that confirmed demand to this recipe
              now. <strong>Future Only</strong> leaves it as-is — only newly confirmed
              demand uses this recipe.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => mutation.reset()}>Cancel</AlertDialogCancel>
            <Button
              type="button"
              variant="outline"
              onClick={() => handleChooseScope("future_only")}
              disabled={mutation.isPending}
            >
              Future Only
            </Button>
            <AlertDialogAction onClick={() => handleChooseScope("apply_existing")}>
              Apply Existing
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
