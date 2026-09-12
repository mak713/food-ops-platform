// Ingredient detail (Phase 4 Plan v4 §4/§13), mirroring CustomerDetailPage/
// ProductDetailPage exactly, including the stale-version Refresh pattern. Deliberately
// shows NO inventory/cost figures (physical quantity, weighted-average/latest/replacement
// unit cost) — those are Phase 5 scope; showing a misleading zero/null value before Phase
// 5 implements real restock/cost workflows would be worse than not showing them at all.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
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
