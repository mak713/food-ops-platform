// Manual physical-quantity adjustment form for Purchased Product Inventory (Phase 5 Plan
// §E) — reuses the same reason vocabulary as Ingredient adjustment (approval decision 11).

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
import type { ManualAdjustmentReason } from "../ingredients/inventoryApi";

const REASON_OPTIONS: { value: ManualAdjustmentReason; label: string }[] = [
  { value: "COUNT_CORRECTION", label: "Count correction" },
  { value: "SPOILAGE_OR_WASTE", label: "Spoilage or waste" },
  { value: "PERSONAL_OR_INTERNAL_USE", label: "Personal or internal use" },
  { value: "DAMAGE", label: "Damage" },
  { value: "OTHER", label: "Other" },
];

const schema = z.object({
  quantity_change: z
    .string()
    .min(1, "Quantity change is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) !== 0, "Quantity change must not be zero"),
  reason: z.string().min(1, "Reason is required"),
  notes: z.string().optional(),
});

export type PurchasedAdjustmentFormValues = z.infer<typeof schema>;

export interface PurchasedAdjustmentInput {
  quantity_change: string;
  reason: ManualAdjustmentReason;
  notes: string | null;
}

interface PurchasedAdjustmentFormProps {
  onSubmit: (values: PurchasedAdjustmentInput) => void;
  isPending: boolean;
  mutationError: unknown;
  onCancel: () => void;
}

export function PurchasedAdjustmentForm({
  onSubmit,
  isPending,
  mutationError,
  onCancel,
}: PurchasedAdjustmentFormProps) {
  const {
    register,
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<PurchasedAdjustmentFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { quantity_change: "", reason: "", notes: "" },
  });

  const submit = handleSubmit((values) => {
    onSubmit({
      quantity_change: values.quantity_change,
      reason: values.reason as ManualAdjustmentReason,
      notes: values.notes || null,
    });
  });

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <div className="flex flex-col gap-1">
        <label htmlFor="quantity_change" className="text-sm font-medium">
          Quantity change
        </label>
        <p className="text-xs text-muted-foreground">Positive to add, negative to remove.</p>
        <Input id="quantity_change" inputMode="decimal" {...register("quantity_change")} />
        {errors.quantity_change && (
          <p className="text-xs text-destructive">{errors.quantity_change.message}</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="reason" className="text-sm font-medium">
          Reason
        </label>
        <Controller
          name="reason"
          control={control}
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger id="reason" className="w-56">
                <SelectValue placeholder="Choose a reason…" />
              </SelectTrigger>
              <SelectContent>
                {REASON_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        />
        {errors.reason && <p className="text-xs text-destructive">{errors.reason.message}</p>}
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
          {isPending ? "Saving…" : "Save adjustment"}
        </Button>
      </div>
    </form>
  );
}
