// Manual Surplus creation (SUR-008; Phase 8 Final Architecture Lock §9, corrected by
// Final Architecture Lock Addendum §B) — the only way to add new physical Surplus
// supply; `POST /surplus/{id}/adjust` is reduction-only. Cost basis is always
// seller-entered here, never silently defaulted — the server rejects a missing
// value rather than fabricating one.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Controller, useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { CurrencyInput } from "../../components/shared/CurrencyInput";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
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
import { productsApi } from "../products/api";
import { surplusApi } from "./api";

const schema = z.object({
  product_id: z.string().min(1, "Product is required"),
  quantity: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Quantity must be positive"),
  unit_cost_basis: z
    .string()
    .min(1, "Unit cost basis is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) >= 0, "Must be a valid amount"),
  usable_through_date: z.string().optional(),
  is_reusable: z.boolean(),
  notes: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

export function SurplusCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const productsQuery = useQuery({
    queryKey: ["products", { for: "surplus-create" }],
    queryFn: () => productsApi.list({ is_active: true, limit: 200 }),
  });
  const producedProducts = (productsQuery.data?.items ?? []).filter(
    (p) => p.product_type === "PRODUCED",
  );

  const {
    control,
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      product_id: "",
      quantity: "",
      unit_cost_basis: "",
      usable_through_date: "",
      is_reusable: false,
      notes: "",
    },
  });

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      surplusApi.create({
        product_id: values.product_id,
        quantity: values.quantity,
        unit_cost_basis: values.unit_cost_basis,
        usable_through_date: values.usable_through_date || null,
        is_reusable: values.is_reusable,
        notes: values.notes || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["surplus"] });
      navigate("/app/surplus");
    },
  });

  const submit = handleSubmit((values) => mutation.mutate(values));

  const genericError =
    mutation.isError && mutation.error instanceof ApiError
      ? mutation.error.body?.error.message
      : mutation.isError
        ? "Something went wrong recording this surplus. Please try again."
        : undefined;

  if (productsQuery.isError) {
    return <ErrorBanner error={productsQuery.error} onRetry={() => productsQuery.refetch()} />;
  }

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Record Surplus</h1>
      <p className="text-sm text-muted-foreground">
        For physical finished goods not tied to a Production Run — e.g. counting stock you
        already have on hand. New supply always goes through this form, never as a top-up to an
        existing lot.
      </p>

      <form className="flex flex-col gap-4" onSubmit={submit}>
        <div className="flex flex-col gap-1">
          <label htmlFor="product_id" className="text-sm font-medium">
            Product
          </label>
          <Controller
            name="product_id"
            control={control}
            render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id="product_id">
                  <SelectValue placeholder="Choose a product…" />
                </SelectTrigger>
                <SelectContent>
                  {producedProducts.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          {errors.product_id && (
            <p className="text-xs text-destructive">{errors.product_id.message}</p>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="quantity" className="text-sm font-medium">
            Quantity
          </label>
          <Input id="quantity" inputMode="decimal" {...register("quantity")} />
          {errors.quantity && (
            <p className="text-xs text-destructive">{errors.quantity.message}</p>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="unit_cost_basis" className="text-sm font-medium">
            Unit cost basis
          </label>
          <p className="text-xs text-muted-foreground">
            The per-unit value to record for this lot — never assumed or fabricated.
          </p>
          <CurrencyInput id="unit_cost_basis" {...register("unit_cost_basis")} />
          {errors.unit_cost_basis && (
            <p className="text-xs text-destructive">{errors.unit_cost_basis.message}</p>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="usable_through_date" className="text-sm font-medium">
            Usable through (optional)
          </label>
          <Input id="usable_through_date" type="date" {...register("usable_through_date")} />
        </div>

        <div className="flex items-center gap-2">
          <input
            id="is_reusable"
            type="checkbox"
            className="size-4"
            {...register("is_reusable")}
          />
          <label htmlFor="is_reusable" className="text-sm font-medium">
            Reusable for future order allocation
          </label>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="notes" className="text-sm font-medium">
            Notes (optional)
          </label>
          <Textarea id="notes" {...register("notes")} />
        </div>

        {genericError && (
          <p role="alert" className="text-sm text-destructive">
            {genericError}
          </p>
        )}

        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => navigate("/app/surplus")}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "Saving…" : "Record Surplus"}
          </Button>
        </div>
      </form>
    </div>
  );
}
