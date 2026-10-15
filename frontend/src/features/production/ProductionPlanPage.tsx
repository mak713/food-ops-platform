// Production Plan / Review (Phase 8 Final Architecture Lock §5/§12) — every
// currently-actionable ProductionRequirement (CALCULATED, genuinely needing
// production, not already covered by an active run), earliest demand date first.
// Read-only review; "Start" links through to the batch-override/acknowledgment flow.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { buttonVariants } from "../../components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { productionApi } from "./api";

export function ProductionPlanPage() {
  const query = useQuery({
    queryKey: ["production-requirements"],
    queryFn: () => productionApi.listRequirements({ limit: 200 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Production Plan</h1>
        <Link to="/app/production/runs" className={buttonVariants({ variant: "outline", size: "sm" })}>
          Active Runs
        </Link>
      </div>

      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : query.isError ? (
        <ErrorBanner error={query.error} onRetry={() => query.refetch()} />
      ) : query.data && query.data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing needs production right now.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product</TableHead>
              <TableHead>Demand date</TableHead>
              <TableHead>Demand remaining</TableHead>
              <TableHead>Recommended batches</TableHead>
              <TableHead>Expected output</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.items.map((req) => (
              <TableRow key={req.id}>
                <TableCell>{req.product_name}</TableCell>
                <TableCell>{req.demand_date}</TableCell>
                <TableCell>{formatQuantityForDisplay(req.production_demand_quantity)}</TableCell>
                <TableCell>{req.recommended_batches ?? "—"}</TableCell>
                <TableCell>
                  {req.expected_output_quantity
                    ? formatQuantityForDisplay(req.expected_output_quantity)
                    : "—"}
                </TableCell>
                <TableCell>
                  <Link
                    to={`/app/production/start/${req.id}`}
                    className={buttonVariants({ variant: "default", size: "sm" })}
                  >
                    Review &amp; Start
                  </Link>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
