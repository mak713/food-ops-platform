// Create/edit Ingredient (Phase 4 Plan v4 §4/§13). Dedicated route, not a modal, mirroring
// CustomerFormPage/ProductFormPage exactly, including the prerequisite-GET guards,
// `retry: false`, and the stale-version Refresh pattern. `measurement_family`/
// `canonical_unit` are immutable after creation — shown as static text in edit mode, a
// Select pair only when creating (mirrors Product.product_type's immutability display).

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Controller, useForm } from "react-hook-form";
import { useNavigate, useParams } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { FormField } from "../../components/shared/FormField";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { MEASUREMENT_FAMILIES, UNIT_OPTIONS_BY_FAMILY } from "../../lib/units";
import { ingredientsApi, type Ingredient, type MeasurementFamily } from "./api";

const schema = z.object({
  name: z.string().min(1, "Name is required"),
  measurement_family: z.enum(["WEIGHT", "VOLUME", "COUNT"]),
  canonical_unit: z.string().min(1, "Canonical unit is required"),
});

type FormValues = z.infer<typeof schema>;

function toFormValues(ingredient?: Ingredient): FormValues {
  return {
    name: ingredient?.name ?? "",
    measurement_family: ingredient?.measurement_family ?? "WEIGHT",
    canonical_unit: ingredient?.canonical_unit ?? "g",
  };
}

export function IngredientFormPage() {
  const { id } = useParams<{ id: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const existingQuery = useQuery({
    queryKey: ["ingredients", id],
    queryFn: () => ingredientsApi.get(id as string),
    enabled: isEdit,
    // A 404 (missing or foreign-tenant) won't resolve on retry — matches the Detail page.
    retry: false,
  });

  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    setValue,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: toFormValues() });

  useEffect(() => {
    if (existingQuery.data) {
      reset(toFormValues(existingQuery.data));
    }
  }, [existingQuery.data, reset]);

  const selectedFamily = watch("measurement_family");

  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      if (isEdit) {
        if (!existingQuery.data) {
          throw new Error("Cannot save: the original record failed to load.");
        }
        return ingredientsApi.update(existingQuery.data.id, {
          version: existingQuery.data.version,
          name: values.name,
        });
      }
      return ingredientsApi.create(values);
    },
    onSuccess: async (ingredient) => {
      await queryClient.invalidateQueries({ queryKey: ["ingredients"] });
      navigate(`/app/inventory/ingredients/${ingredient.id}`);
    },
  });

  const isStaleVersion =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";

  const handleRefreshAfterStaleVersion = async () => {
    mutation.reset();
    const fresh = await existingQuery.refetch();
    if (fresh.data) {
      reset(toFormValues(fresh.data));
    }
  };

  if (isEdit && existingQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (isEdit && existingQuery.isError) {
    const isNotFound = existingQuery.error instanceof ApiError && existingQuery.error.status === 404;
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
    return <ErrorBanner error={existingQuery.error} onRetry={() => existingQuery.refetch()} />;
  }

  return (
    <div className="mx-auto flex max-w-md flex-col gap-6">
      <h1 className="text-xl font-semibold">{isEdit ? "Edit Ingredient" : "New Ingredient"}</h1>

      <form
        className="flex flex-col gap-4"
        onSubmit={handleSubmit((values) => mutation.mutate(values))}
      >
        <FormField id="name" label="Name" error={errors.name?.message}>
          <Input id="name" {...register("name")} />
        </FormField>

        {isEdit ? (
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium">Measurement family / canonical unit</span>
            <span className="text-sm text-muted-foreground">
              {existingQuery.data?.measurement_family} — {existingQuery.data?.canonical_unit}
              {" (cannot be changed after creation)"}
            </span>
          </div>
        ) : (
          <>
            <FormField
              id="measurement_family"
              label="Measurement family"
              error={errors.measurement_family?.message}
            >
              <Controller
                name="measurement_family"
                control={control}
                render={({ field }) => (
                  <Select
                    value={field.value}
                    onValueChange={(value) => {
                      const family = value as MeasurementFamily;
                      field.onChange(family);
                      // canonical_unit belongs to a closed per-family set — an
                      // incompatible previously-selected unit must not survive a family
                      // change. Deterministically pick that family's first supported
                      // unit, matching the same "always a valid default" convention
                      // toFormValues() already uses for a brand-new form.
                      setValue("canonical_unit", UNIT_OPTIONS_BY_FAMILY[family][0].value, {
                        shouldDirty: true,
                      });
                    }}
                  >
                    <SelectTrigger id="measurement_family">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MEASUREMENT_FAMILIES.map((family) => (
                        <SelectItem key={family} value={family}>
                          {family}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </FormField>

            <FormField
              id="canonical_unit"
              label="Canonical unit"
              error={errors.canonical_unit?.message}
            >
              <Controller
                name="canonical_unit"
                control={control}
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="canonical_unit">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {UNIT_OPTIONS_BY_FAMILY[selectedFamily].map((unit) => (
                        <SelectItem key={unit.value} value={unit.value}>
                          {unit.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </FormField>
          </>
        )}

        {isStaleVersion && (
          <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            <p role="alert">
              This record changed since you opened it. Refresh the latest version and
              review your changes before saving again.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleRefreshAfterStaleVersion}
            >
              Refresh
            </Button>
          </div>
        )}

        {mutation.isError && !isStaleVersion && (
          <p role="alert" className="text-sm text-destructive">
            {mutation.error instanceof ApiError
              ? mutation.error.body?.error.message
              : "Something went wrong. Please try again."}
          </p>
        )}

        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? "Saving…" : "Save"}
        </Button>
      </form>
    </div>
  );
}
