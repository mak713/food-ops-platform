// Surplus transaction history (SUR-001..003; Phase 8 Final Architecture Lock §9) —
// every PRODUCTION_OUTPUT / ORDER_FULFILLMENT / MANUAL_ADJUSTMENT row for one lot,
// newest first. Mirrors IngredientInventoryHistoryPage's list-only convention.

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { surplusApi } from "./api";

const TYPE_LABEL: Record<string, string> = {
  PRODUCTION_OUTPUT: "Production output",
  ORDER_FULFILLMENT: "Order fulfillment",
  MANUAL_ADJUSTMENT: "Manual adjustment",
};

export function SurplusHistoryPage() {
  const { id } = useParams<{ id: string }>();
  const surplusId = id as string;

  const lotQuery = useQuery({
    queryKey: ["surplus", surplusId],
    queryFn: () => surplusApi.get(surplusId),
    retry: false,
  });
  const transactionsQuery = useQuery({
    queryKey: ["surplus", surplusId, "transactions"],
    queryFn: () => surplusApi.listTransactions(surplusId, { limit: 200 }),
    retry: false,
  });

  if (lotQuery.isLoading || transactionsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (lotQuery.isError) {
    return <ErrorBanner error={lotQuery.error} onRetry={() => lotQuery.refetch()} />;
  }
  if (transactionsQuery.isError) {
    return (
      <ErrorBanner error={transactionsQuery.error} onRetry={() => transactionsQuery.refetch()} />
    );
  }
  const lot = lotQuery.data;
  if (!lot) return null;

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Surplus History — {lot.product_name}</h1>
      <p className="text-sm text-muted-foreground">
        Current physical quantity: {formatQuantityForDisplay(lot.physical_quantity)}
      </p>

      {transactionsQuery.data && transactionsQuery.data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No transactions recorded yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Quantity change</TableHead>
              <TableHead>Reason / notes</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {transactionsQuery.data?.items.map((txn) => (
              <TableRow key={txn.id}>
                <TableCell>{new Date(txn.created_at).toLocaleString()}</TableCell>
                <TableCell>
                  <Badge variant="outline">{TYPE_LABEL[txn.transaction_type]}</Badge>
                </TableCell>
                <TableCell>
                  {Number(txn.quantity_change) > 0 ? "+" : ""}
                  {formatQuantityForDisplay(txn.quantity_change)}
                </TableCell>
                <TableCell>
                  {txn.reason && <p>{txn.reason}</p>}
                  {txn.notes && <p className="text-xs text-muted-foreground">{txn.notes}</p>}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
