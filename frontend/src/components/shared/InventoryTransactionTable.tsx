// Shared paginated inventory-transaction-history table (Phase 5 Plan §F) — reused by both
// the per-resource "History" pages and the Inventory-module's tabbed History hub, for
// both Ingredient and Purchased Product Inventory transactions (same response shape on
// both sides, two distinct typed queries — never a merged/polymorphic backend query).
// Mirrors RecipeHistoryPage.tsx's offset/limit/"Showing X–Y of Z" pagination pattern.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import type { InventoryTransaction } from "../../features/ingredients/inventoryApi";
import type { PageResponse } from "../../features/customers/api";
import { formatDecimal } from "../../lib/decimal";
import { Button } from "../ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";

const PAGE_SIZE = 20;

interface InventoryTransactionTableProps {
  queryKey: readonly unknown[];
  fetchPage: (params: { limit: number; offset: number }) => Promise<PageResponse<InventoryTransaction>>;
}

export function InventoryTransactionTable({ queryKey, fetchPage }: InventoryTransactionTableProps) {
  const [offset, setOffset] = useState(0);

  const query = useQuery({
    queryKey: [...queryKey, { offset }],
    queryFn: () => fetchPage({ limit: PAGE_SIZE, offset }),
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (query.isError) {
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const data = query.data;
  if (!data) {
    return null;
  }

  const hasPrevious = offset > 0;
  const hasNext = offset + data.items.length < data.total;

  return (
    <div className="flex flex-col gap-3">
      {data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No inventory transactions yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Type</TableHead>
              <TableHead>Quantity change</TableHead>
              <TableHead>Unit cost</TableHead>
              <TableHead>Total cost</TableHead>
              <TableHead>Supplier / store</TableHead>
              <TableHead>Reason / notes</TableHead>
              <TableHead>Date</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.items.map((txn) => (
              <TableRow key={txn.id}>
                <TableCell>{txn.transaction_type}</TableCell>
                <TableCell>{formatDecimal(txn.quantity_change)}</TableCell>
                <TableCell>{txn.unit_cost ? formatDecimal(txn.unit_cost) : "—"}</TableCell>
                <TableCell>{txn.total_cost ? formatDecimal(txn.total_cost) : "—"}</TableCell>
                <TableCell>{txn.supplier_text ?? "—"}</TableCell>
                <TableCell>
                  {txn.reason ?? "—"}
                  {txn.notes && (
                    <p className="text-xs text-muted-foreground">{txn.notes}</p>
                  )}
                </TableCell>
                <TableCell>{new Date(txn.created_at).toLocaleString()}</TableCell>
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
          {data.total === 0
            ? "0 transactions"
            : `Showing ${offset + 1}–${offset + data.items.length} of ${data.total}`}
        </span>
      </div>
    </div>
  );
}
