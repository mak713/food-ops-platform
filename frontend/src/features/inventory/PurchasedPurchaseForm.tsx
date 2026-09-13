// Shared quantity/cost form used by both Initial Balance and Restock for Purchased
// Product Inventory (Phase 5 Plan §E) — no unit selector at all: Purchased Product
// Inventory has no measurement_family/canonical_unit; quantity is a plain Decimal count
// of the Product's own units, not necessarily a whole number (approval decision 9).

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";

const schema = z.object({
  quantity: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Quantity must be greater than 0"),
  unit_cost: z
    .string()
    .min(1, "Unit cost is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) >= 0, "Unit cost must be 0 or greater"),
  supplier_text: z.string().optional(),
  notes: z.string().optional(),
});

export type PurchasedPurchaseFormValues = z.infer<typeof schema>;

export interface PurchasedPurchaseInput {
  quantity: string;
  unit_cost: string;
  supplier_text: string | null;
  notes: string | null;
}

interface PurchasedPurchaseFormProps {
  onSubmit: (values: PurchasedPurchaseInput) => void;
  isPending: boolean;
  mutationError: unknown;
  submitLabel: string;
  onCancel: () => void;
}

export function PurchasedPurchaseForm({
  onSubmit,
  isPending,
  mutationError,
  submitLabel,
  onCancel,
}: PurchasedPurchaseFormProps) {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<PurchasedPurchaseFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { quantity: "", unit_cost: "", supplier_text: "", notes: "" },
  });

  const submit = handleSubmit((values) => {
    onSubmit({
      quantity: values.quantity,
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
        <label htmlFor="unit_cost" className="text-sm font-medium">
          Unit cost
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
