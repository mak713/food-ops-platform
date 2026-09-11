// Customer detail (Phase 3 plan v3 §13). Deliberately has NO Order-history section at
// all — Phase 3 never queries Orders, so implying that knowledge (even as an empty
// "no orders yet" state) would assert something the app hasn't actually checked (§13).
// AC-CUS-007 (derived history/metrics) is fully deferred to Phase 6+.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { customersApi } from "./api";

export function CustomerDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["customers", id],
    queryFn: () => customersApi.get(id as string),
    // A 404 (missing or foreign-tenant) won't resolve on retry — matches AuthContext's
    // `me` query, which disables retry for the identical reason.
    retry: false,
  });

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["customers", id] }),
      queryClient.invalidateQueries({ queryKey: ["customers"] }),
    ]);

  const archiveMutation = useMutation({
    mutationFn: () => customersApi.archive(id as string, query.data!.version),
    onSuccess: invalidate,
  });
  const reactivateMutation = useMutation({
    mutationFn: () => customersApi.reactivate(id as string, query.data!.version),
    onSuccess: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: () => customersApi.remove(id as string, query.data!.version),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["customers"] });
      navigate("/app/customers");
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
          <h1 className="text-xl font-semibold">Customer not found</h1>
          <Button variant="outline" onClick={() => navigate("/app/customers")}>
            Back to customers
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const customer = query.data;
  if (!customer) {
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
        <h1 className="text-xl font-semibold">{customer.name}</h1>
        <Badge variant={customer.is_active ? "default" : "secondary"}>
          {customer.is_active ? "Active" : "Archived"}
        </Badge>
      </div>

      <dl className="flex flex-col gap-2 text-sm">
        <div>
          <dt className="text-muted-foreground">Phone</dt>
          <dd>{customer.phone ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Email</dt>
          <dd>{customer.email ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Preferred contact method</dt>
          <dd>{customer.preferred_contact_method ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Notes</dt>
          <dd className="whitespace-pre-wrap">{customer.notes ?? "—"}</dd>
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
          onClick={() => navigate(`/app/customers/${customer.id}/edit`)}
        >
          Edit
        </Button>

        {customer.is_active ? (
          <AlertDialog>
            <AlertDialogTrigger className={buttonVariants({ variant: "outline" })}>
              Archive
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Archive this customer?</AlertDialogTitle>
                <AlertDialogDescription>
                  Archived customers no longer appear in the active list. You can reactivate
                  them at any time.
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

        <AlertDialog>
          <AlertDialogTrigger className={buttonVariants({ variant: "destructive" })}>
            Delete
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete this customer?</AlertDialogTitle>
              <AlertDialogDescription>
                This cannot be undone. If this customer has order history, deletion will be
                blocked — archive them instead.
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
