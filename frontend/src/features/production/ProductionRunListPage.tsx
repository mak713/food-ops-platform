// Active Runs (Phase 8 Final Architecture Lock §12) — every ProductionRun,
// filterable by status; defaults to IN_PRODUCTION so the seller lands on what
// needs attention.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { buttonVariants } from "../../components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { productionApi, type ProductionRunStatus } from "./api";

const STATUS_LABEL: Record<ProductionRunStatus, string> = {
  IN_PRODUCTION: "In Production",
  COMPLETED: "Completed",
  CANCELED: "Canceled",
};

const STATUS_VARIANT: Record<ProductionRunStatus, "default" | "secondary" | "outline"> = {
  IN_PRODUCTION: "default",
  COMPLETED: "secondary",
  CANCELED: "outline",
};

export function ProductionRunListPage() {
  const [status, setStatus] = useState<ProductionRunStatus | "ALL">("IN_PRODUCTION");

  const query = useQuery({
    queryKey: ["production-runs", status],
    queryFn: () =>
      productionApi.listRuns({ limit: 200, status: status === "ALL" ? undefined : status }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Active Runs</h1>
        <Link to="/app/production" className={buttonVariants({ variant: "outline", size: "sm" })}>
          Production Plan
        </Link>
      </div>

      <Select value={status} onValueChange={(v) => setStatus(v as ProductionRunStatus | "ALL")}>
        <SelectTrigger className="w-56">
          <SelectValue placeholder="Filter by status" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="IN_PRODUCTION">In Production</SelectItem>
          <SelectItem value="COMPLETED">Completed</SelectItem>
          <SelectItem value="CANCELED">Canceled</SelectItem>
          <SelectItem value="ALL">All</SelectItem>
        </SelectContent>
      </Select>

      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : query.isError ? (
        <ErrorBanner error={query.error} onRetry={() => query.refetch()} />
      ) : query.data && query.data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No production runs here.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Batches</TableHead>
              <TableHead>Planned output</TableHead>
              <TableHead>Started</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.items.map((run) => (
              <TableRow key={run.id}>
                <TableCell>{run.product_name}</TableCell>
                <TableCell>
                  <Badge variant={STATUS_VARIANT[run.status]}>{STATUS_LABEL[run.status]}</Badge>
                </TableCell>
                <TableCell>{run.planned_batches}</TableCell>
                <TableCell>{formatQuantityForDisplay(run.planned_output_quantity)}</TableCell>
                <TableCell>{new Date(run.started_at).toLocaleString()}</TableCell>
                <TableCell>
                  <Link
                    to={`/app/production/runs/${run.id}`}
                    className={buttonVariants({ variant: "outline", size: "sm" })}
                  >
                    View
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
