// Production Plan / Review (Phase 8 Final Architecture Lock §5/§12) — every
// currently-actionable ProductionRequirement (CALCULATED, genuinely needing
// production, not already covered by an active run), earliest demand date first.
// Read-only review; "Start" links through to the batch-override/acknowledgment flow.
//
// Phase 9 (Spec §12.11, §20.13) adds shortage and affected-demand visibility: the per-
// Ingredient shortage shown here is the backend's ADR-130 derivation "as of this
// requirement's demand date" — the same one the Shopping List uses — and the affected
// Orders are the ones feeding the requirement. Nothing is computed in React.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { buttonVariants } from "../../components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { formatDate, formatTime } from "../../lib/format";
import { productionApi, type ProductionRequirement } from "./api";

function ShortageCell({ req }: { req: ProductionRequirement }) {
  const short = req.ingredient_needs.filter((n) => Number(n.shortage_quantity) > 0);
  if (short.length === 0) {
    return <span>No shortage</span>;
  }
  return (
    <div className="flex flex-col gap-1">
      <Badge variant="destructive">Ingredient shortage</Badge>
      <ul className="text-xs">
        {short.map((n) => (
          <li key={n.ingredient_id}>
            {n.ingredient_name}: short {formatQuantityForDisplay(n.shortage_quantity)}{" "}
            {n.canonical_unit} (needs {formatQuantityForDisplay(n.required_quantity)})
          </li>
        ))}
      </ul>
      <Link
        to="/app/production/shopping-list"
        className="text-xs underline-offset-4 hover:underline"
      >
        See Shopping List
      </Link>
    </div>
  );
}

function AffectedOrdersCell({ req }: { req: ProductionRequirement }) {
  if (req.affected_orders.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <ul className="text-xs">
      {req.affected_orders.map((o) => (
        <li key={o.order_id}>
          <Link to={`/app/orders/${o.order_id}`} className="underline-offset-4 hover:underline">
            {o.order_number}
          </Link>{" "}
          · {o.customer_name ?? "Guest"}
          {o.fulfillment_date && ` · ${formatDate(o.fulfillment_date)}`}
          {o.fulfillment_time && ` ${formatTime(o.fulfillment_time)}`} ·{" "}
          {formatQuantityForDisplay(o.demand_quantity)} units
        </li>
      ))}
    </ul>
  );
}

export function ProductionPlanPage() {
  const query = useQuery({
    queryKey: ["production-requirements"],
    queryFn: () => productionApi.listRequirements({ limit: 200 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Production Plan</h1>
        <div className="flex gap-2">
          <Link
            to="/app/production/shopping-list"
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            Shopping List
          </Link>
          <Link
            to="/app/production/runs"
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            Active Runs
          </Link>
        </div>
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
              <TableHead>Shortage</TableHead>
              <TableHead>Affected orders</TableHead>
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
                  <ShortageCell req={req} />
                </TableCell>
                <TableCell>
                  <AffectedOrdersCell req={req} />
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
