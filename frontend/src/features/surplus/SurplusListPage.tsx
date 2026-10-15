// Surplus list (SUR-001..003, SUR-008; Phase 8 Final Architecture Lock §9) — every
// balance, run-sourced and manually-created alike, with a link to each lot's
// manual-adjustment form and transaction history.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { buttonVariants } from "../../components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { surplusApi } from "./api";

export function SurplusListPage() {
  const query = useQuery({
    queryKey: ["surplus"],
    queryFn: () => surplusApi.list({ limit: 200 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Surplus</h1>
        <Link to="/app/surplus/new" className={buttonVariants({ variant: "default" })}>
          Record Surplus
        </Link>
      </div>

      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : query.isError ? (
        <ErrorBanner error={query.error} onRetry={() => query.refetch()} />
      ) : query.data && query.data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No surplus recorded yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product</TableHead>
              <TableHead>Physical quantity</TableHead>
              <TableHead>Unit cost basis</TableHead>
              <TableHead>Source</TableHead>
              <TableHead>Usable through</TableHead>
              <TableHead>Reusable</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.items.map((lot) => (
              <TableRow key={lot.id}>
                <TableCell>{lot.product_name}</TableCell>
                <TableCell>{formatQuantityForDisplay(lot.physical_quantity)}</TableCell>
                <TableCell>${lot.unit_cost_basis}</TableCell>
                <TableCell>
                  <Badge variant={lot.source_production_run_id ? "default" : "outline"}>
                    {lot.source_production_run_id ? "Production" : "Manual"}
                  </Badge>
                </TableCell>
                <TableCell>{lot.usable_through_date ?? "—"}</TableCell>
                <TableCell>{lot.is_reusable ? "Yes" : "No"}</TableCell>
                <TableCell>
                  <div className="flex gap-2">
                    <Link
                      to={`/app/surplus/${lot.id}/adjust`}
                      className={buttonVariants({ variant: "outline", size: "sm" })}
                    >
                      Adjust
                    </Link>
                    <Link
                      to={`/app/surplus/${lot.id}/history`}
                      className={buttonVariants({ variant: "outline", size: "sm" })}
                    >
                      History
                    </Link>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
