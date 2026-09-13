// Ingredient detail (Phase 4 Plan v4 §4/§13; Phase 5 Plan §D adds the Inventory section
// below), mirroring CustomerDetailPage/ProductDetailPage exactly, including the
// stale-version Refresh pattern. Initial Balance/Restock/Replacement-Cost are hidden for
// an archived Ingredient (backend-enforced 409s; hiding them here is just UX — Manual
// Adjustment and History remain available regardless, per Phase 5 Plan §D/§F approval
// decision 6).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
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
import { Button, buttonVariants } from "../../components/ui/button";
import { formatDecimal } from "../../lib/decimal";
import { ingredientsApi } from "./api";

export function IngredientDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["ingredients", id],
    queryFn: () => ingredientsApi.get(id as string),
    retry: false,
  });

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["ingredients", id] }),
      queryClient.invalidateQueries({ queryKey: ["ingredients"] }),
    ]);

  const archiveMutation = useMutation({
    mutationFn: () => ingredientsApi.archive(id as string, query.data!.version),
    onSuccess: invalidate,
  });
  const reactivateMutation = useMutation({
    mutationFn: () => ingredientsApi.reactivate(id as string, query.data!.version),
    onSuccess: invalidate,
  });
  // Closed imperatively after an expected rejection (e.g. 409 INGREDIENT_HAS_REFERENCES)
  // — AlertDialogAction has no built-in "confirm and close" behavior of its own, so
  // without this the dialog would otherwise stay open over the error it caused, leaving
  // the blocking message hidden behind the modal overlay.
  const deleteDialogActionsRef = useRef<{ close: () => void; unmount: () => void } | null>(null);
  const deleteMutation = useMutation({
    mutationFn: () => ingredientsApi.remove(id as string, query.data!.version),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["ingredients"] });
      navigate("/app/inventory/ingredients");
    },
    onError: () => {
      deleteDialogActionsRef.current?.close();
    },
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (query.isError) {
    const isNotFound = query.error instanceof ApiError && query.error.status === 404;
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
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const ingredient = query.data;
  if (!ingredient) {
    return null;
  }
  const actionError = archiveMutation.error ?? reactivateMutation.error ?? deleteMutation.error;
  const isStale = (err: unknown) =>
    err instanceof ApiError && err.body?.error.code === "STALE_VERSION";
  const staleVersionOccurred =
    isStale(archiveMutation.error) ||
    isStale(reactivateMutation.error) ||
    isStale(deleteMutation.error);

  const handleRefreshAfterStaleVersion = async () => {
    archiveMutation.reset();
    reactivateMutation.reset();
    deleteMutation.reset();
    await query.refetch();
  };

  // A generic "needs reconciliation" attention state (Phase 5 Plan approval decision 2)
  // — a negative physical quantity is a legitimate outcome of manual adjustment (or, in
  // a later phase, real production consumption); this never blocks anything, it only
  // makes the state visible rather than rendering a bare negative number unremarked.
  const isNegativeBalance = Number(ingredient.physical_quantity) < 0;
  // Display-only heuristic for whether to offer "Initial Balance" instead of assuming
  // Restock is always the first action — the backend's own one-time-only rule (whether
  // any InventoryTransaction has ever been created) is the actual source of truth.
  const neverInitialized =
    Number(ingredient.physical_quantity) === 0 &&
    Number(ingredient.weighted_average_unit_cost) === 0 &&
    ingredient.latest_purchase_unit_cost == null &&
    ingredient.replacement_unit_cost == null;
  // Initial Balance / Restock / Replacement-Cost maintenance are all backend-blocked for
  // an archived Ingredient (Phase 5 Plan §F approval decision 6) — hidden here to match;
  // Manual Adjustment and History remain available regardless of active state.

  return (
    <div className="flex max-w-md flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{ingredient.name}</h1>
        <Badge variant={ingredient.is_active ? "default" : "secondary"}>
          {ingredient.is_active ? "Active" : "Archived"}
        </Badge>
      </div>

      <dl className="flex flex-col gap-2 text-sm">
        <div>
          <dt className="text-muted-foreground">Measurement family</dt>
          <dd>{ingredient.measurement_family}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Canonical unit</dt>
          <dd>{ingredient.canonical_unit}</dd>
        </div>
      </dl>

      <div className="flex flex-col gap-3 rounded-md border border-border p-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Inventory</h2>
          {isNegativeBalance && <Badge variant="destructive">Needs reconciliation</Badge>}
        </div>
        <dl className="flex flex-col gap-2 text-sm">
          <div>
            <dt className="text-muted-foreground">Physical quantity</dt>
            <dd className={isNegativeBalance ? "font-medium text-destructive" : undefined}>
              {formatDecimal(ingredient.physical_quantity)} {ingredient.canonical_unit}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Weighted-average unit cost</dt>
            <dd>{formatDecimal(ingredient.weighted_average_unit_cost)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Latest purchase cost</dt>
            <dd>
              {ingredient.latest_purchase_unit_cost != null
                ? formatDecimal(ingredient.latest_purchase_unit_cost)
                : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Replacement cost</dt>
            <dd>
              {ingredient.effective_replacement_cost != null ? (
                <>
                  {formatDecimal(ingredient.effective_replacement_cost)}{" "}
                  <span className="text-xs text-muted-foreground">
                    (
                    {ingredient.replacement_unit_cost != null
                      ? "manually set"
                      : "following latest purchase cost"}
                    )
                  </span>
                </>
              ) : (
                "—"
              )}
            </dd>
          </div>
        </dl>

        <div className="flex flex-wrap items-center gap-3 text-sm">
          {neverInitialized && ingredient.is_active && (
            <Link
              to={`/app/inventory/ingredients/${ingredient.id}/initial-balance`}
              className="underline-offset-4 hover:underline"
            >
              Initial Balance
            </Link>
          )}
          {ingredient.is_active && (
            <Link
              to={`/app/inventory/ingredients/${ingredient.id}/restock`}
              className="underline-offset-4 hover:underline"
            >
              Restock
            </Link>
          )}
          <Link
            to={`/app/inventory/ingredients/${ingredient.id}/adjust`}
            className="underline-offset-4 hover:underline"
          >
            Adjust
          </Link>
          {ingredient.is_active && (
            <Link
              to={`/app/inventory/ingredients/${ingredient.id}/replacement-cost`}
              className="underline-offset-4 hover:underline"
            >
              Replacement Cost
            </Link>
          )}
          <Link
            to={`/app/inventory/ingredients/${ingredient.id}/history`}
            className="underline-offset-4 hover:underline"
          >
            History
          </Link>
        </div>
      </div>

      {staleVersionOccurred ? (
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
      ) : (
        actionError && (
          <p role="alert" className="text-sm text-destructive">
            {actionError instanceof ApiError
              ? actionError.body?.error.message
              : "Something went wrong. Please try again."}
          </p>
        )
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          onClick={() => navigate(`/app/inventory/ingredients/${ingredient.id}/edit`)}
        >
          Edit
        </Button>

        {ingredient.is_active ? (
          <AlertDialog>
            <AlertDialogTrigger className={buttonVariants({ variant: "outline" })}>
              Archive
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Archive this ingredient?</AlertDialogTitle>
                <AlertDialogDescription>
                  Archived ingredients no longer appear in the active list. You can
                  reactivate them at any time.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={() => archiveMutation.mutate()}>
                  Archive
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        ) : (
          <Button variant="outline" onClick={() => reactivateMutation.mutate()}>
            Reactivate
          </Button>
        )}

        <AlertDialog actionsRef={deleteDialogActionsRef}>
          <AlertDialogTrigger className={buttonVariants({ variant: "destructive" })}>
            Delete
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete this ingredient?</AlertDialogTitle>
              <AlertDialogDescription>
                This cannot be undone. If this ingredient is used elsewhere, deletion will
                be blocked — archive it instead.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                className={buttonVariants({ variant: "destructive" })}
                onClick={() => deleteMutation.mutate()}
              >
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}
