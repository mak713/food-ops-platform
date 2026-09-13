// Replacement Cost maintenance for an Ingredient (Phase 5 Plan §D, correction-pass
// finding 1) — shows the current effective value and whether it is a manually-maintained
// override or is following Latest Purchase Cost, lets the seller set a new override, and
// lets them clear an existing one back to automatic fallback. The unit basis is
// explicitly labeled as the Ingredient's own canonical unit (finding 1's unit-labeling
// requirement).

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { formatDecimal } from "../../lib/decimal";

const schema = z.object({
  replacement_unit_cost: z
    .string()
    .min(1, "Replacement cost is required")
    .refine(
      (v) => !Number.isNaN(Number(v)) && Number(v) >= 0,
      "Replacement cost must be 0 or greater",
    ),
});

export type IngredientReplacementCostFormValues = z.infer<typeof schema>;

interface IngredientReplacementCostFormProps {
  canonicalUnit: string;
  latestPurchaseUnitCost: string | null;
  replacementUnitCost: string | null;
  effectiveReplacementCost: string | null;
  onSetOverride: (value: string) => void;
  onClearOverride: () => void;
  isPending: boolean;
  mutationError: unknown;
  onCancel: () => void;
}

export function IngredientReplacementCostForm({
  canonicalUnit,
  latestPurchaseUnitCost,
  replacementUnitCost,
  effectiveReplacementCost,
  onSetOverride,
  onClearOverride,
  isPending,
  mutationError,
  onCancel,
}: IngredientReplacementCostFormProps) {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<IngredientReplacementCostFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { replacement_unit_cost: replacementUnitCost ?? "" },
  });

  const isManuallySet = replacementUnitCost != null;

  const submit = handleSubmit((values) => {
    onSetOverride(values.replacement_unit_cost);
  });

  return (
    <div className="flex flex-col gap-4">
      <dl className="flex flex-col gap-2 text-sm">
        <div>
          <dt className="text-muted-foreground">Effective replacement cost</dt>
          <dd>
            {effectiveReplacementCost != null ? (
              <>
                {formatDecimal(effectiveReplacementCost)} per {canonicalUnit}{" "}
                <span className="text-xs text-muted-foreground">
                  ({isManuallySet ? "manually set" : "following latest purchase cost"})
                </span>
              </>
            ) : (
              "— (unknown/unset)"
            )}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Latest purchase cost</dt>
          <dd>
            {latestPurchaseUnitCost != null
              ? `${formatDecimal(latestPurchaseUnitCost)} per ${canonicalUnit}`
              : "—"}
          </dd>
        </div>
      </dl>

      <form className="flex flex-col gap-4" onSubmit={submit}>
        <div className="flex flex-col gap-1">
          <label htmlFor="replacement_unit_cost" className="text-sm font-medium">
            Replacement cost override (per {canonicalUnit})
          </label>
          <p className="text-xs text-muted-foreground">
            Explicitly set a replacement cost for this ingredient. Leave it as-is and use
            "Clear override" below to instead follow Latest Purchase Cost automatically.
          </p>
          <Input
            id="replacement_unit_cost"
            inputMode="decimal"
            {...register("replacement_unit_cost")}
          />
          {errors.replacement_unit_cost && (
            <p className="text-xs text-destructive">{errors.replacement_unit_cost.message}</p>
          )}
        </div>

        {mutationError ? (
          <p role="alert" className="text-sm text-destructive">
            {mutationError instanceof ApiError
              ? mutationError.body?.error.message
              : "Something went wrong. Please try again."}
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={onClearOverride}
            disabled={isPending || !isManuallySet}
          >
            Clear override
          </Button>
          <Button type="submit" disabled={isPending}>
            {isPending ? "Saving…" : "Save override"}
          </Button>
        </div>
      </form>
    </div>
  );
}
