// Recipe revision history (Phase 4 Plan v4 §7/§13) — paginated per ADR-101, same
// {items,total,limit,offset} consumption pattern as every other list page in the app, so
// revisions beyond the first page (default limit 50) remain reachable.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
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

const PAGE_SIZE = 20;

export function RecipeHistoryPage() {
  const { id } = useParams<{ id: string }>();
  const productId = id as string;
  const navigate = useNavigate();
  const [offset, setOffset] = useState(0);

  const query = useQuery({
    queryKey: ["products", productId, "recipe", "revisions", { offset }],
    queryFn: () => recipeApi.listRevisions(productId, { limit: PAGE_SIZE, offset }),
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
          <h1 className="text-xl font-semibold">No recipe yet</h1>
          <Button variant="outline" onClick={() => navigate(`/app/products/${productId}`)}>
            Back to product
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const data = query.data;
  if (!data) {
    return null;
  }

  const hasPrevious = offset > 0;
  const hasNext = offset + data.items.length < data.total;

  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Recipe Revision History</h1>
        <Link
          to={`/app/products/${productId}`}
          className="text-sm text-muted-foreground underline-offset-4 hover:underline"
        >
          Back to product
        </Link>
      </div>

      {data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No revisions yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Revision</TableHead>
              <TableHead>Yield</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.items.map((revision) => (
              <TableRow key={revision.id}>
                <TableCell>
                  <Link
                    to={`/app/products/${productId}/recipe/revisions/${revision.id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    #{revision.revision_number}
                  </Link>
                </TableCell>
                <TableCell>{formatDecimal(revision.yield_quantity)}</TableCell>
                <TableCell>
                  <Badge variant={revision.is_current ? "default" : "secondary"}>
                    {revision.is_current ? "Current" : "Historical"}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={!hasPrevious}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!hasNext}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </Button>
        <span className="text-xs text-muted-foreground">
          {data.total === 0 ? "0 revisions" : `Showing ${offset + 1}–${offset + data.items.length} of ${data.total}`}
        </span>
      </div>
    </div>
  );
}
