// Read-only single historical (or current) revision detail (Phase 4 Plan v4 §5/§13) —
// revisions are immutable, so this page has no edit affordance at all.

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { formatDecimal } from "../../lib/decimal";
import { recipeApi } from "./recipeApi";

export function RecipeRevisionDetailPage() {
  const { id, revisionId } = useParams<{ id: string; revisionId: string }>();
  const productId = id as string;
  const navigate = useNavigate();

  const query = useQuery({
    queryKey: ["products", productId, "recipe", "revisions", revisionId],
    queryFn: () => recipeApi.getRevision(productId, revisionId as string),
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
          <h1 className="text-xl font-semibold">Revision not found</h1>
          <Button variant="outline" onClick={() => navigate(`/app/products/${productId}/recipe/history`)}>
            Back to history
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const revision = query.data;
  if (!revision) {
    return null;
  }

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Revision #{revision.revision_number}</h1>
        <Badge variant={revision.is_current ? "default" : "secondary"}>
          {revision.is_current ? "Current" : "Historical"}
        </Badge>
      </div>

      <dl className="flex flex-col gap-2 text-sm">
        <div>
          <dt className="text-muted-foreground">Yield</dt>
          <dd>{formatDecimal(revision.yield_quantity)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Active time</dt>
          <dd>{revision.active_time_minutes} minutes</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Elapsed time</dt>
          <dd>{revision.elapsed_time_minutes ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Notes</dt>
          <dd className="whitespace-pre-wrap">{revision.notes ?? "—"}</dd>
        </div>
      </dl>

      <div className="flex flex-col gap-2">
        <h2 className="text-lg font-semibold">Ingredients</h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Ingredient</TableHead>
              <TableHead>Quantity</TableHead>
              <TableHead>Unit</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {revision.ingredients.map((line) => (
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

      <Link
        to={`/app/products/${productId}/recipe/history`}
        className="text-sm text-muted-foreground underline-offset-4 hover:underline"
      >
        Back to history
      </Link>
    </div>
  );
}
