// Product detail (Phase 3 plan v3 §13/§15) — the only place Selling Options are ever
// managed, for both a freshly-created (possibly empty) Product and an existing one alike.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { formatDecimal } from "../../lib/decimal";
import { productsApi } from "./api";
import { NewSellingOptionForm } from "./NewSellingOptionForm";
import { recipeApi } from "./recipeApi";
import { SellingOptionRow } from "./SellingOptionRow";

export function ProductDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["products", id],
    queryFn: () => productsApi.get(id as string),
    // A 404 (missing or foreign-tenant) won't resolve on retry — matches AuthContext's
    // `me` query, which disables retry for the identical reason.
    retry: false,
  });

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["products", id] }),
      queryClient.invalidateQueries({ queryKey: ["products"] }),
    ]);

  const archiveMutation = useMutation({
    mutationFn: () => productsApi.archive(id as string, query.data!.version),
    onSuccess: invalidate,
  });
  const reactivateMutation = useMutation({
    mutationFn: () => productsApi.reactivate(id as string, query.data!.version),
    onSuccess: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: () => productsApi.remove(id as string, query.data!.version),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["products"] });
      navigate("/app/products");
    },
  });

  const recipeQuery = useQuery({
    queryKey: ["products", id, "recipe"],
    queryFn: () => recipeApi.get(id as string),
    enabled: query.data?.product_type === "PRODUCED",
    retry: false,
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (query.isError) {
    const isNotFound = query.error instanceof ApiError && query.error.status === 404;
    if (isNotFound) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">Product not found</h1>
          <Button variant="outline" onClick={() => navigate("/app/products")}>
            Back to products
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const product = query.data;
  if (!product) {
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
    <div className="flex max-w-3xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">{product.name}</h1>
          <p className="text-sm text-muted-foreground">
            {product.product_type === "PRODUCED" ? "Produced" : "Purchased"}
          </p>
        </div>
        <Badge variant={product.is_active ? "default" : "secondary"}>
          {product.is_active ? "Active" : "Archived"}
        </Badge>
      </div>

      <dl className="flex flex-col gap-2 text-sm">
        <div>
          <dt className="text-muted-foreground">Description</dt>
          <dd className="whitespace-pre-wrap">{product.description ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Default packaging cost</dt>
          <dd>{product.default_packaging_cost}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Can reuse surplus</dt>
          <dd>{product.can_reuse_surplus ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Default surplus usable days</dt>
          <dd>{product.default_surplus_usable_days ?? "—"}</dd>
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
        <Button variant="outline" onClick={() => navigate(`/app/products/${product.id}/edit`)}>
          Edit
        </Button>

        {product.is_active ? (
          <AlertDialog>
            <AlertDialogTrigger className={buttonVariants({ variant: "outline" })}>
              Archive
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Archive this product?</AlertDialogTitle>
                <AlertDialogDescription>
                  Its Selling Options keep their own individual active/archived state — this
                  does not change them. You can reactivate the product at any time.
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
              <AlertDialogTitle>Delete this product?</AlertDialogTitle>
              <AlertDialogDescription>
                This cannot be undone. If it has a recipe or operational history, deletion
                will be blocked — archive it instead.
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

      <div className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Selling Options</h2>

        {product.selling_options.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No selling options yet — add one below.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Quantity</TableHead>
                <TableHead>Price</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {product.selling_options.map((option) => (
                <SellingOptionRow key={option.id} productId={product.id} option={option} />
              ))}
            </TableBody>
          </Table>
        )}

        <NewSellingOptionForm productId={product.id} />
      </div>

      {product.product_type === "PRODUCED" && (
        <div className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Recipe</h2>

          {recipeQuery.isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : recipeQuery.isError ? (
            recipeQuery.error instanceof ApiError && recipeQuery.error.status === 404 ? (
              <div className="flex items-center gap-3">
                <p className="text-sm text-muted-foreground">No recipe yet.</p>
                <Link
                  to={`/app/products/${product.id}/recipe/new`}
                  className="text-sm underline-offset-4 hover:underline"
                >
                  Create Recipe
                </Link>
              </div>
            ) : (
              <ErrorBanner error={recipeQuery.error} onRetry={() => recipeQuery.refetch()} />
            )
          ) : recipeQuery.data ? (
            <>
              <p className="text-sm font-medium">{recipeQuery.data.name}</p>
              <dl className="flex flex-col gap-2 text-sm">
                <div>
                  <dt className="text-muted-foreground">Current revision</dt>
                  <dd>#{recipeQuery.data.current_revision.revision_number}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Yield</dt>
                  <dd>{formatDecimal(recipeQuery.data.current_revision.yield_quantity)}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Active time</dt>
                  <dd>{recipeQuery.data.current_revision.active_time_minutes} minutes</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Elapsed time</dt>
                  <dd>
                    {recipeQuery.data.current_revision.elapsed_time_minutes != null
                      ? `${recipeQuery.data.current_revision.elapsed_time_minutes} minutes`
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Notes</dt>
                  <dd className="whitespace-pre-wrap">
                    {recipeQuery.data.current_revision.notes ?? "—"}
                  </dd>
                </div>
              </dl>

              <div className="flex flex-col gap-2">
                <span className="text-sm font-medium">Ingredients</span>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Ingredient</TableHead>
                      <TableHead>Quantity</TableHead>
                      <TableHead>Unit</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {recipeQuery.data.current_revision.ingredients.map((line) => (
                      <TableRow key={line.id}>
                        <TableCell>
                          {line.ingredient_name}
                          {!line.ingredient_is_active && (
                            <Badge variant="secondary" className="ml-2">
                              Archived
                            </Badge>
                          )}
                        </TableCell>
                        <TableCell>{formatDecimal(line.quantity)}</TableCell>
                        <TableCell>{line.unit}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <Link
                  to={`/app/products/${product.id}/recipe/edit`}
                  className="text-sm underline-offset-4 hover:underline"
                >
                  Edit Recipe
                </Link>
                <Link
                  to={`/app/products/${product.id}/recipe/history`}
                  className="text-sm underline-offset-4 hover:underline"
                >
                  View History
                </Link>
              </div>
            </>
          ) : null}
        </div>
      )}
    </div>
  );
}
