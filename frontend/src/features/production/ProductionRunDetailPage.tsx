// Production Run detail (RUN-002/007..010; Phase 8 Final Architecture Lock §6) —
// run snapshot, the Finish form (actual output + per-ingredient actual-quantity
// overrides, defaulting to planned), and Cancel. Finish's response carries
// transient, non-persisted flags (`negative_inventory_ingredient_ids`,
// `is_shortfall`) that only exist on the mutation result, not on a later GET — kept
// in local state so the reconciliation/shortfall messaging survives the
// query-invalidation refetch that follows a successful Finish.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "../../components/ui/alert-dialog";
import { Badge } from "../../components/ui/badge";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button, buttonVariants } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { productionApi, type ProductionFinishResponse } from "./api";

const STATUS_LABEL: Record<string, string> = {
  IN_PRODUCTION: "In Production",
  COMPLETED: "Completed",
  CANCELED: "Canceled",
};

export function ProductionRunDetailPage() {
  const { id } = useParams<{ id: string }>();
  const runId = id as string;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [actualOutput, setActualOutput] = useState("");
  const [ingredientOverrides, setIngredientOverrides] = useState<Record<string, string>>({});
  const [finishResult, setFinishResult] = useState<ProductionFinishResponse | null>(null);

  const query = useQuery({
    queryKey: ["production-runs", runId],
    queryFn: () => productionApi.get(runId),
    retry: false,
  });

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["production-runs", runId] }),
      queryClient.invalidateQueries({ queryKey: ["production-runs"] }),
      queryClient.invalidateQueries({ queryKey: ["production-requirements"] }),
    ]);

  const finishMutation = useMutation({
    mutationFn: () =>
      productionApi.finish(runId, {
        version: query.data!.version,
        actual_output_quantity: actualOutput,
        ingredient_actual_quantities:
          Object.keys(ingredientOverrides).length > 0 ? ingredientOverrides : null,
      }),
    onSuccess: async (result) => {
      setFinishResult(result);
      await invalidate();
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => productionApi.cancel(runId, { version: query.data!.version }),
    onSuccess: async () => {
      await invalidate();
      navigate("/app/production/runs");
    },
  });

  const finishGenericError =
    finishMutation.isError && finishMutation.error instanceof ApiError
      ? finishMutation.error.body?.error.message
      : finishMutation.isError
        ? "Something went wrong finishing this run. Please try again."
        : undefined;
  const cancelGenericError =
    cancelMutation.isError && cancelMutation.error instanceof ApiError
      ? cancelMutation.error.body?.error.message
      : cancelMutation.isError
        ? "Something went wrong canceling this run. Please try again."
        : undefined;

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError) {
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }
  const run = query.data;
  if (!run) return null;

  const negativeInventoryNames = (finishResult?.negative_inventory_ingredient_ids ?? [])
    .map((ingId) => run.run_ingredients.find((ri) => ri.ingredient_id === ingId)?.ingredient_name)
    .filter((name): name is string => Boolean(name));

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">{run.product_name}</h1>
          <p className="text-sm text-muted-foreground">
            Started {new Date(run.started_at).toLocaleString()}
          </p>
        </div>
        <Badge variant={run.status === "IN_PRODUCTION" ? "default" : "secondary"}>
          {STATUS_LABEL[run.status]}
        </Badge>
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 rounded-md border p-3 text-sm sm:grid-cols-3">
        <div className="flex justify-between sm:block">
          <dt className="text-muted-foreground">Batches</dt>
          <dd>{run.planned_batches}</dd>
        </div>
        <div className="flex justify-between sm:block">
          <dt className="text-muted-foreground">Planned output</dt>
          <dd>{formatQuantityForDisplay(run.planned_output_quantity)}</dd>
        </div>
        <div className="flex justify-between sm:block">
          <dt className="text-muted-foreground">Actual output</dt>
          <dd>
            {run.actual_output_quantity ? formatQuantityForDisplay(run.actual_output_quantity) : "—"}
          </dd>
        </div>
        <div className="flex justify-between sm:block">
          <dt className="text-muted-foreground">Est. production cost</dt>
          <dd>${run.estimated_total_production_cost}</dd>
        </div>
        <div className="flex justify-between sm:block">
          <dt className="text-muted-foreground">Actual production cost</dt>
          <dd>{run.actual_total_production_cost ? `$${run.actual_total_production_cost}` : "—"}</dd>
        </div>
        <div className="flex justify-between sm:block">
          <dt className="text-muted-foreground">Completed / canceled</dt>
          <dd>
            {run.completed_at
              ? new Date(run.completed_at).toLocaleString()
              : run.canceled_at
                ? new Date(run.canceled_at).toLocaleString()
                : "—"}
          </dd>
        </div>
      </dl>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Ingredients</h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Ingredient</TableHead>
              <TableHead>Planned</TableHead>
              <TableHead>Actual</TableHead>
              <TableHead>Cost basis</TableHead>
              <TableHead>Actual cost</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {run.run_ingredients.map((ri) => (
              <TableRow key={ri.ingredient_id}>
                <TableCell>{ri.ingredient_name}</TableCell>
                <TableCell>{formatQuantityForDisplay(ri.planned_quantity)}</TableCell>
                <TableCell>
                  {run.status === "IN_PRODUCTION" ? (
                    <Input
                      className="w-28"
                      inputMode="decimal"
                      placeholder={ri.planned_quantity}
                      value={ingredientOverrides[ri.ingredient_id] ?? ""}
                      onChange={(e) =>
                        setIngredientOverrides((prev) => {
                          const next = { ...prev };
                          if (e.target.value) {
                            next[ri.ingredient_id] = e.target.value;
                          } else {
                            delete next[ri.ingredient_id];
                          }
                          return next;
                        })
                      }
                    />
                  ) : ri.actual_quantity ? (
                    formatQuantityForDisplay(ri.actual_quantity)
                  ) : (
                    "—"
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant="outline">{ri.cost_basis_source}</Badge>
                </TableCell>
                <TableCell>{ri.actual_total_cost ? `$${ri.actual_total_cost}` : "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </section>

      {run.status === "IN_PRODUCTION" && (
        <section className="flex flex-col gap-3 rounded-md border border-dashed p-3">
          <h2 className="text-lg font-semibold">Finish Production</h2>
          <div className="flex flex-col gap-1">
            <label htmlFor="actual_output" className="text-sm font-medium">
              Actual usable output
            </label>
            <p className="text-xs text-muted-foreground">
              Enter 0 if this batch produced no usable output — ingredients already consumed are
              still recorded, and any remaining demand stays active for a future run.
            </p>
            <Input
              id="actual_output"
              className="w-40"
              inputMode="decimal"
              value={actualOutput}
              onChange={(e) => setActualOutput(e.target.value)}
            />
          </div>

          {finishGenericError && (
            <p role="alert" className="text-sm text-destructive">
              {finishGenericError}
            </p>
          )}

          <div>
            <Button
              type="button"
              disabled={finishMutation.isPending || !actualOutput}
              onClick={() => finishMutation.mutate()}
            >
              {finishMutation.isPending ? "Finishing…" : "Finish Production"}
            </Button>
          </div>

          <AlertDialog>
            <AlertDialogTrigger
              className={buttonVariants({ variant: "outline", size: "sm" })}
              disabled={cancelMutation.isPending}
            >
              Cancel Run
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Cancel this production run?</AlertDialogTitle>
                <AlertDialogDescription>
                  No ingredients have been consumed yet — canceling simply frees this demand for a
                  future run. This cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep run</AlertDialogCancel>
                <AlertDialogAction
                  className={buttonVariants({ variant: "destructive" })}
                  onClick={() => cancelMutation.mutate()}
                >
                  Cancel run
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
          {cancelGenericError && (
            <p role="alert" className="text-sm text-destructive">
              {cancelGenericError}
            </p>
          )}
        </section>
      )}

      {finishResult && (
        <section className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
          <h2 className="font-semibold">Finish results</h2>
          {finishResult.is_shortfall && (
            <p role="alert">
              This run did not fully cover its outstanding demand — the remaining amount stays
              active and can be covered by a future run.
            </p>
          )}
          {negativeInventoryNames.length > 0 && (
            <p role="alert">
              These ingredients now have a negative recorded balance and may need a physical
              count/restock: {negativeInventoryNames.join(", ")}.
            </p>
          )}
          {!finishResult.is_shortfall && negativeInventoryNames.length === 0 && (
            <p>Finished cleanly — demand fully covered, no negative ingredient balances.</p>
          )}
        </section>
      )}
    </div>
  );
}
