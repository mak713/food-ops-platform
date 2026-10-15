// Manual Surplus adjustment (SUR-008; Phase 8 Final Architecture Lock Addendum §B) —
// reduction-only; `POST /surplus/{id}/adjust` rejects a positive delta (new supply
// always goes through SurplusCreatePage instead). A reduction that would cut below
// the lot's currently-allocated quantity is a consequential reconciliation that may
// demote Ready orders — the server rejects it once with
// `SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT`, surfacing the
// exact consequences and a fingerprint; resubmitting with that fingerprint commits
// the same reconciliation it already computed.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Controller, useForm } from "react-hook-form";
import { useNavigate, useParams } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { StaleVersionPanel } from "../../components/shared/StaleVersionPanel";
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
import { formatQuantityForDisplay } from "../../lib/decimal";
import type { ManualAdjustmentReason } from "../ingredients/inventoryApi";
import { surplusApi } from "./api";

const REASON_OPTIONS: { value: ManualAdjustmentReason; label: string }[] = [
  { value: "COUNT_CORRECTION", label: "Count correction" },
  { value: "SPOILAGE_OR_WASTE", label: "Spoilage or waste" },
  { value: "PERSONAL_OR_INTERNAL_USE", label: "Personal or internal use" },
  { value: "DAMAGE", label: "Damage" },
  { value: "OTHER", label: "Other" },
];

const schema = z.object({
  quantity_removed: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Must be a positive amount"),
  reason: z.string().min(1, "Reason is required"),
  notes: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

export function SurplusAdjustPage() {
  const { id } = useParams<{ id: string }>();
  const surplusId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["surplus", surplusId],
    queryFn: () => surplusApi.get(surplusId),
    retry: false,
  });

  const {
    control,
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { quantity_removed: "", reason: "", notes: "" },
  });

  const mutation = useMutation({
    mutationFn: (input: {
      quantity_delta: string;
      reason: ManualAdjustmentReason;
      notes: string | null;
      acknowledged_warning_fingerprint?: string | null;
    }) =>
      surplusApi.adjust(surplusId, { version: query.data!.version, ...input }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["surplus", surplusId] }),
        queryClient.invalidateQueries({ queryKey: ["surplus"] }),
      ]);
      navigate("/app/surplus");
    },
  });

  const reconciliationIssue =
    mutation.error instanceof ApiError &&
    mutation.error.body?.error.code ===
      "SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT"
      ? mutation.error.body.error.issues[0]
      : undefined;
  const isStale =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";
  const genericError =
    mutation.isError && !reconciliationIssue && !isStale
      ? mutation.error instanceof ApiError
        ? mutation.error.body?.error.message
        : "Something went wrong adjusting this surplus. Please try again."
      : undefined;

  const submit = handleSubmit((values) => {
    mutation.mutate({
      quantity_delta: `-${values.quantity_removed}`,
      reason: values.reason as ManualAdjustmentReason,
      notes: values.notes || null,
    });
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError) {
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }
  const lot = query.data;
  if (!lot) return null;

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Adjust Surplus — {lot.product_name}</h1>
      <p className="text-sm text-muted-foreground">
        Current physical quantity: {formatQuantityForDisplay(lot.physical_quantity)}. This form
        only removes quantity — record new supply from the Surplus list instead.
      </p>

      <form className="flex flex-col gap-4" onSubmit={submit}>
        <div className="flex flex-col gap-1">
          <label htmlFor="quantity_removed" className="text-sm font-medium">
            Quantity to remove
          </label>
          <Input id="quantity_removed" inputMode="decimal" {...register("quantity_removed")} />
          {errors.quantity_removed && (
            <p className="text-xs text-destructive">{errors.quantity_removed.message}</p>
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

        {reconciliationIssue && (
          <div className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
            <p className="font-semibold">This will demote Ready orders</p>
            <p role="alert">{reconciliationIssue.message}</p>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={mutation.isPending}
              onClick={handleSubmit((values) =>
                mutation.mutate({
                  quantity_delta: `-${values.quantity_removed}`,
                  reason: values.reason as ManualAdjustmentReason,
                  notes: values.notes || null,
                  acknowledged_warning_fingerprint: reconciliationIssue.details
                    .fingerprint as string,
                }),
              )}
            >
              Adjust anyway
            </Button>
          </div>
        )}

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
            {mutation.isPending ? "Saving…" : "Save adjustment"}
          </Button>
        </div>
      </form>

      {isStale && (
        <StaleVersionPanel
          onRefresh={async () => {
            mutation.reset();
            await query.refetch();
          }}
        />
      )}
    </div>
  );
}
