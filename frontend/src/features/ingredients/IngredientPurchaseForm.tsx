// Shared quantity/unit/cost form used by both Initial Balance and Restock for an
// Ingredient (Phase 5 Plan §D) — same fields, same normalization behavior server-side;
// only the submit endpoint and label differ (mirrors RecipeContentForm's Create/Edit
// sharing pattern). The unit `<Select>` is filtered to the Ingredient's own
// measurement_family, exactly like a Recipe ingredient-line's unit selector.

import { zodResolver } from "@hookform/resolvers/zod";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError } from "../../api/client";
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
import { UNIT_OPTIONS_BY_FAMILY } from "../../lib/units";
import type { MeasurementFamily } from "./api";

const schema = z.object({
  quantity: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Quantity must be greater than 0"),
  unit: z.string().min(1, "Unit is required"),
  unit_cost: z
    .string()
    .min(1, "Unit cost is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) >= 0, "Unit cost must be 0 or greater"),
  supplier_text: z.string().optional(),
  notes: z.string().optional(),
});

export type IngredientPurchaseFormValues = z.infer<typeof schema>;

export interface IngredientPurchaseInput {
  quantity: string;
  unit: string;
  unit_cost: string;
  supplier_text: string | null;
  notes: string | null;
}

interface IngredientPurchaseFormProps {
  measurementFamily: MeasurementFamily;
  defaultValues: IngredientPurchaseFormValues;
  onSubmit: (values: IngredientPurchaseInput) => void;
  isPending: boolean;
  mutationError: unknown;
  submitLabel: string;
  onCancel: () => void;
}

export function IngredientPurchaseForm({
  measurementFamily,
  defaultValues,
  onSubmit,
  isPending,
  mutationError,
  submitLabel,
  onCancel,
}: IngredientPurchaseFormProps) {
  const {
    register,
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<IngredientPurchaseFormValues>({ resolver: zodResolver(schema), defaultValues });

  const unitOptions = UNIT_OPTIONS_BY_FAMILY[measurementFamily];

  const submit = handleSubmit((values) => {
    onSubmit({
      quantity: values.quantity,
      unit: values.unit,
      unit_cost: values.unit_cost,
      supplier_text: values.supplier_text || null,
      notes: values.notes || null,
    });
  });

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <div className="flex flex-col gap-1">
        <label htmlFor="quantity" className="text-sm font-medium">
          Quantity
        </label>
        <Input id="quantity" inputMode="decimal" {...register("quantity")} />
        {errors.quantity && <p className="text-xs text-destructive">{errors.quantity.message}</p>}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="unit" className="text-sm font-medium">
          Unit
        </label>
        <Controller
          name="unit"
          control={control}
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger id="unit" className="w-40">
                <SelectValue placeholder="Unit" />
              </SelectTrigger>
              <SelectContent>
                {unitOptions.map((unit) => (
                  <SelectItem key={unit.value} value={unit.value}>
                    {unit.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        />
        {errors.unit && <p className="text-xs text-destructive">{errors.unit.message}</p>}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="unit_cost" className="text-sm font-medium">
          Unit cost (per unit entered above)
        </label>
        <Input id="unit_cost" inputMode="decimal" {...register("unit_cost")} />
        {errors.unit_cost && (
          <p className="text-xs text-destructive">{errors.unit_cost.message}</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="supplier_text" className="text-sm font-medium">
          Supplier (optional)
        </label>
        <Input id="supplier_text" {...register("supplier_text")} />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="notes" className="text-sm font-medium">
          Notes (optional)
        </label>
        <Textarea id="notes" {...register("notes")} />
      </div>

      {mutationError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationError instanceof ApiError
            ? mutationError.body?.error.message
            : "Something went wrong. Please try again."}
        </p>
      ) : null}

      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={isPending}>
          Cancel
        </Button>
        <Button type="submit" disabled={isPending}>
          {isPending ? "Saving…" : submitLabel}
        </Button>
      </div>
    </form>
  );
}
