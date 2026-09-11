// Inline "add a selling option" form on ProductDetailPage — works whether the product
// has zero existing options (the normal post-creation state, Phase 3 plan v3 §9) or many.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { sellingOptionsApi } from "./api";

// Checkpoint 3 remediation: real numeric validation, not just "non-empty string" — a
// value like "0" or "-1" or "abc" must be blocked client-side before any fetch, matching
// the backend's own constraints (quantity_units > 0; price/packaging_cost >= 0; sort_order
// a valid integer). `Number(v)` (not `parseInt`) so trailing garbage like "12abc" is
// rejected outright instead of silently truncated to 12.
const schema = z.object({
  name: z.string().min(1, "Name is required"),
  quantity_units: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Quantity must be greater than 0"),
  price: z
    .string()
    .min(1, "Price is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) >= 0, "Price must be 0 or greater"),
  packaging_cost: z
    .string()
    .optional()
    .refine(
      (v) => !v || (!Number.isNaN(Number(v)) && Number(v) >= 0),
      "Packaging cost must be 0 or greater",
    ),
  sort_order: z
    .string()
    .optional()
    .refine((v) => !v || Number.isInteger(Number(v)), "Sort order must be a whole number"),
});

type FormValues = z.infer<typeof schema>;

export function NewSellingOptionForm({ productId }: { productId: string }) {
  const queryClient = useQueryClient();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      sellingOptionsApi.create(productId, {
        name: values.name,
        quantity_units: values.quantity_units,
        price: values.price,
        packaging_cost: values.packaging_cost || "0",
        // Safe now: Zod already validated this is blank or a genuine integer string.
        sort_order: values.sort_order ? Number(values.sort_order) : 0,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["products", productId] });
      reset();
    },
  });

  return (
    <form
      className="flex flex-wrap items-end gap-2 rounded-md border border-border p-3"
      onSubmit={handleSubmit((values) => mutation.mutate(values))}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="new-option-name" className="text-xs text-muted-foreground">
          Name
        </label>
        <Input id="new-option-name" className="w-32" {...register("name")} />
        {errors.name && <p className="text-xs text-destructive">{errors.name.message}</p>}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new-option-quantity" className="text-xs text-muted-foreground">
          Quantity
        </label>
        <Input
          id="new-option-quantity"
          className="w-24"
          inputMode="decimal"
          {...register("quantity_units")}
        />
        {errors.quantity_units && (
          <p className="text-xs text-destructive">{errors.quantity_units.message}</p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new-option-price" className="text-xs text-muted-foreground">
          Price
        </label>
        <Input id="new-option-price" className="w-24" inputMode="decimal" {...register("price")} />
        {errors.price && <p className="text-xs text-destructive">{errors.price.message}</p>}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new-option-packaging-cost" className="text-xs text-muted-foreground">
          Packaging cost
        </label>
        <Input
          id="new-option-packaging-cost"
          className="w-24"
          inputMode="decimal"
          {...register("packaging_cost")}
        />
        {errors.packaging_cost && (
          <p className="text-xs text-destructive">{errors.packaging_cost.message}</p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new-option-sort-order" className="text-xs text-muted-foreground">
          Sort order
        </label>
        <Input
          id="new-option-sort-order"
          className="w-16"
          inputMode="numeric"
          {...register("sort_order")}
        />
        {errors.sort_order && (
          <p className="text-xs text-destructive">{errors.sort_order.message}</p>
        )}
      </div>
      <Button type="submit" size="sm" disabled={mutation.isPending}>
        {mutation.isPending ? "Adding…" : "Add Selling Option"}
      </Button>
      {mutation.isError && (
        <p role="alert" className="w-full text-xs text-destructive">
          {mutation.error instanceof ApiError
            ? mutation.error.body?.error.message
            : "Something went wrong."}
        </p>
      )}
    </form>
  );
}
