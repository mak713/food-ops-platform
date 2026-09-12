// Recipe creation (Phase 4 Plan v4 §5/§13) — Recipe + Revision 1 + all ingredient lines
// are created atomically in one request; there is no separate "create empty recipe" step.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { ingredientsApi } from "../ingredients/api";
import { productsApi } from "./api";
import { recipeApi, type RecipeContentInput } from "./recipeApi";
import { RecipeContentForm, type IngredientOption } from "./RecipeContentForm";
import { useState } from "react";

export function RecipeCreatePage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

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
    mutationFn: (content: RecipeContentInput) => recipeApi.create(productId, { name, ...content }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["products", productId] });
      navigate(`/app/products/${productId}`);
    },
  });

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
        mutationError={mutation.error}
        submitLabel="Create Recipe"
        onSubmit={(content) => {
          if (!name.trim()) {
            setNameError("Recipe name is required");
            return;
          }
          mutation.mutate(content);
        }}
        onCancel={() => navigate(`/app/products/${productId}`)}
      />
    </div>
  );
}
