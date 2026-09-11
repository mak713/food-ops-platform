// Create/edit Product — Product-level fields only, never Selling Options (Phase 3 plan
// v3 §9): create is a single-resource POST, and Selling Options are managed exclusively
// on the Detail page afterward, for both a freshly-created and an existing Product alike.
// `product_type` is immutable after creation (§10) — shown as static text in edit mode,
// a Select only when creating.

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
import { Textarea } from "../../components/ui/textarea";
import { productsApi, type Product, type ProductType } from "./api";

// Checkpoint 3 remediation: client-side numeric validation, not a `parseInt`-in-the-
// mutation-function afterthought. `Number(v)` (not `parseInt`) is used deliberately —
// `parseInt("12abc")` silently truncates to `12`; `Number("12abc")` is `NaN` and
// correctly fails validation instead of being accepted with data quietly discarded.
const schema = z.object({
  name: z.string().min(1, "Name is required"),
  product_type: z.enum(["PRODUCED", "PURCHASED"]),
  description: z.string().optional(),
  default_packaging_cost: z
    .string()
    .min(1, "Default packaging cost is required")
    .refine(
      (v) => !Number.isNaN(Number(v)) && Number(v) >= 0,
      "Default packaging cost must be 0 or greater",
    ),
  can_reuse_surplus: z.boolean().optional(),
  default_surplus_usable_days: z
    .string()
    .optional()
    .refine(
      (v) => !v || (Number.isInteger(Number(v)) && Number(v) > 0),
      "Default surplus usable days must be a whole number greater than 0",
    ),
});

type FormValues = z.infer<typeof schema>;

function toFormValues(product?: Product): FormValues {
  return {
    name: product?.name ?? "",
    product_type: product?.product_type ?? "PRODUCED",
    description: product?.description ?? "",
    default_packaging_cost: product?.default_packaging_cost ?? "0",
    can_reuse_surplus: product?.can_reuse_surplus ?? false,
    default_surplus_usable_days: product?.default_surplus_usable_days?.toString() ?? "",
  };
}

export function ProductFormPage() {
  const { id } = useParams<{ id: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const existingQuery = useQuery({
    queryKey: ["products", id],
    queryFn: () => productsApi.get(id as string),
    enabled: isEdit,
    // A 404 (missing or foreign-tenant) won't resolve on retry — matches the Detail page.
    retry: false,
  });

  const {
    register,
    control,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: toFormValues() });

  useEffect(() => {
    if (existingQuery.data) {
      reset(toFormValues(existingQuery.data));
    }
  }, [existingQuery.data, reset]);

  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      // Safe now: Zod already guaranteed this is either blank or a valid positive
      // integer string before submission was ever allowed to reach here.
      const surplusDays = values.default_surplus_usable_days
        ? Number(values.default_surplus_usable_days)
        : null;

      // Checkpoint 3 remediation: in edit mode this must NEVER silently fall through to
      // create just because `existingQuery.data` happens to be absent — the render
      // guards below bail out before the form (and this mutation) ever renders when the
      // prerequisite GET failed. This throw is a defense-in-depth backstop.
      if (isEdit) {
        if (!existingQuery.data) {
          throw new Error("Cannot save: the original record failed to load.");
        }
        return productsApi.update(existingQuery.data.id, {
          version: existingQuery.data.version,
          name: values.name,
          description: values.description || null,
          default_packaging_cost: values.default_packaging_cost,
          can_reuse_surplus: values.can_reuse_surplus ?? false,
          default_surplus_usable_days: surplusDays,
        });
      }
      return productsApi.create({
        name: values.name,
        product_type: values.product_type,
        description: values.description || null,
        default_packaging_cost: values.default_packaging_cost,
        can_reuse_surplus: values.can_reuse_surplus ?? false,
        default_surplus_usable_days: surplusDays,
      });
    },
    onSuccess: async (product) => {
      await queryClient.invalidateQueries({ queryKey: ["products"] });
      navigate(`/app/products/${product.id}`);
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

  // --- Edit-mode prerequisite-GET guards (Checkpoint 3 remediation) — must run BEFORE
  // the form renders, so a failed fetch can never fall through to a form whose submit
  // handler would otherwise attempt to create instead of update.
  if (isEdit && existingQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (isEdit && existingQuery.isError) {
    const isNotFound = existingQuery.error instanceof ApiError && existingQuery.error.status === 404;
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
    return <ErrorBanner error={existingQuery.error} onRetry={() => existingQuery.refetch()} />;
  }

  return (
    <div className="mx-auto flex max-w-md flex-col gap-6">
      <h1 className="text-xl font-semibold">{isEdit ? "Edit Product" : "New Product"}</h1>

      <form
        className="flex flex-col gap-4"
        onSubmit={handleSubmit((values) => mutation.mutate(values))}
      >
        <FormField id="name" label="Name" error={errors.name?.message}>
          <Input id="name" {...register("name")} />
        </FormField>

        {isEdit ? (
          <div className="flex flex-col gap-1">
            <span className="text-sm font-medium">Type</span>
            <span className="text-sm text-muted-foreground">
              {existingQuery.data?.product_type === "PRODUCED" ? "Produced" : "Purchased"}
              {" — cannot be changed after creation"}
            </span>
          </div>
        ) : (
          <FormField id="product_type" label="Type" error={errors.product_type?.message}>
            <Controller
              name="product_type"
              control={control}
              render={({ field }) => (
                <Select
                  value={field.value}
                  onValueChange={(value) => field.onChange(value as ProductType)}
                >
                  <SelectTrigger id="product_type">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="PRODUCED">Produced</SelectItem>
                    <SelectItem value="PURCHASED">Purchased</SelectItem>
                  </SelectContent>
                </Select>
              )}
            />
          </FormField>
        )}

        <FormField id="description" label="Description" error={errors.description?.message}>
          <Textarea id="description" {...register("description")} />
        </FormField>

        <FormField
          id="default_packaging_cost"
          label="Default packaging cost"
          error={errors.default_packaging_cost?.message}
        >
          <Input
            id="default_packaging_cost"
            inputMode="decimal"
            {...register("default_packaging_cost")}
          />
        </FormField>

        <div className="flex items-center gap-2">
          <input
            id="can_reuse_surplus"
            type="checkbox"
            className="size-4"
            {...register("can_reuse_surplus")}
          />
          <label htmlFor="can_reuse_surplus" className="text-sm font-medium">
            Can reuse surplus
          </label>
        </div>

        <FormField
          id="default_surplus_usable_days"
          label="Default surplus usable days"
          error={errors.default_surplus_usable_days?.message}
        >
          <Input
            id="default_surplus_usable_days"
            inputMode="numeric"
            {...register("default_surplus_usable_days")}
          />
        </FormField>

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
