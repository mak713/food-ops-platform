// Presentational Dashboard sections (Spec §12.15, DASH-001..005). Every section renders
// server-derived values only; status is always conveyed with readable text, never by
// colour alone (Spec §12.17). Empty sections are not rendered (DASH-002) — the page
// decides which to mount.

import { Link } from "react-router-dom";
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
import {
  formatDate,
  formatInstantInTimezone,
  formatMinutes,
  formatMoney,
  formatTime,
} from "../../lib/format";
import type {
  ActiveRun,
  DashboardAlert,
  FulfillmentItem,
  ManualWorkload,
  ProductionItem,
  Snapshot,
  UnsatisfiedLine,
  UpcomingDay,
} from "./api";

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <h2 id={id} className="text-base font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

// ------------------------------------------------------------------ Attention Required

export function AttentionSection({ alerts }: { alerts: DashboardAlert[] }) {
  return (
    <Section id="dash-attention" title="Attention Required">
      <ul className="flex flex-col gap-3">
        {alerts.map((alert) => (
          <li
            key={alert.code}
            className="rounded-md border border-border p-3"
            data-testid={`alert-${alert.code}`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={alert.severity === "WARNING" ? "destructive" : "secondary"}>
                {alert.severity === "WARNING" ? "Warning" : "Notice"}
              </Badge>
              <h3 className="text-sm font-semibold">{alert.title}</h3>
              <Link
                to={alert.destination_path}
                className={buttonVariants({ variant: "outline", size: "sm" }) + " ml-auto"}
              >
                {alert.destination_label}
              </Link>
            </div>
            <p className="mt-1 text-sm">{alert.condition}</p>
            <p className="text-sm text-muted-foreground">{alert.impact}</p>
            {alert.items.length > 0 && (
              <ul className="mt-2 flex flex-col gap-0.5 text-sm">
                {alert.items.map((item, index) => (
                  <li key={`${item.label}-${index}`}>
                    {item.path ? (
                      <Link
                        to={item.path}
                        className="font-medium underline-offset-4 hover:underline"
                      >
                        {item.label}
                      </Link>
                    ) : (
                      <span className="font-medium">{item.label}</span>
                    )}
                    {item.detail && <span className="text-muted-foreground"> — {item.detail}</span>}
                  </li>
                ))}
                {alert.items_truncated && (
                  <li className="text-muted-foreground">
                    …and {alert.count - alert.items.length} more
                  </li>
                )}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}

// ------------------------------------------------------------------ Active Production

export function ActiveProductionSection({
  runs,
  timezone,
}: {
  runs: ActiveRun[];
  timezone: string;
}) {
  return (
    <Section id="dash-active" title="Active Production">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Product</TableHead>
            <TableHead>Batches</TableHead>
            <TableHead>Planned output</TableHead>
            <TableHead>Started</TableHead>
            <TableHead>Elapsed</TableHead>
            <TableHead>Guidance</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => (
            <TableRow key={run.run_id}>
              <TableCell className="font-medium">{run.product_name}</TableCell>
              <TableCell>{run.planned_batches}</TableCell>
              <TableCell>{formatQuantityForDisplay(run.planned_output_quantity)}</TableCell>
              <TableCell>{formatInstantInTimezone(run.started_at, timezone)}</TableCell>
              <TableCell>{formatMinutes(run.elapsed_minutes)}</TableCell>
              <TableCell>
                {run.expected_finish_at
                  ? run.is_past_expected
                    ? "Running longer than the planned time"
                    : `Expected around ${formatInstantInTimezone(run.expected_finish_at, timezone)}`
                  : "No planned duration"}
              </TableCell>
              <TableCell>
                <Link
                  to={`/app/production/runs/${run.run_id}`}
                  className={buttonVariants({ variant: "outline", size: "sm" })}
                >
                  Open run
                </Link>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Section>
  );
}

// ---------------------------------------------------------------- Today's Production

const TIMING_LABEL: Record<ProductionItem["timing"], string> = {
  DUE_TODAY: "Due today",
  ADVANCE_PREP: "Advance prep",
  START_OVERDUE: "Start overdue",
};

export function TodaysProductionSection({
  items,
  manualWorkload,
  timezone,
}: {
  items: ProductionItem[];
  manualWorkload: ManualWorkload | null;
  timezone: string;
}) {
  return (
    <Section id="dash-production" title="Today's Production">
      {items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product</TableHead>
              <TableHead>When</TableHead>
              <TableHead>To make</TableHead>
              <TableHead>Batches</TableHead>
              <TableHead>Suggested start</TableHead>
              <TableHead>Supplies</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.requirement_id}>
                <TableCell className="font-medium">{item.product_name}</TableCell>
                <TableCell>
                  {TIMING_LABEL[item.timing]}
                  {item.timing === "ADVANCE_PREP" && (
                    <span className="block text-xs text-muted-foreground">
                      for {formatDate(item.demand_date)}
                    </span>
                  )}
                </TableCell>
                <TableCell>{formatQuantityForDisplay(item.production_demand_quantity)}</TableCell>
                <TableCell>{item.recommended_batches ?? "—"}</TableCell>
                <TableCell>
                  {item.suggested_start_at
                    ? formatInstantInTimezone(item.suggested_start_at, timezone)
                    : "No suggested start time"}
                </TableCell>
                <TableCell>
                  {item.has_shortage ? (
                    <Badge variant="destructive">Ingredient shortage</Badge>
                  ) : (
                    "OK"
                  )}
                </TableCell>
                <TableCell>
                  <Link
                    to={`/app/production/start/${item.requirement_id}`}
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
      {manualWorkload && (
        <p className="text-sm" data-testid="manual-workload">
          Manual custom-item work today:{" "}
          <strong>{formatMinutes(manualWorkload.total_minutes)}</strong> across{" "}
          {manualWorkload.line_count} {manualWorkload.line_count === 1 ? "item" : "items"} ( your
          own estimate — not a recipe projection).
        </p>
      )}
    </Section>
  );
}

// -------------------------------------------------------------- Today's Fulfillments

const REASON_LABEL: Record<UnsatisfiedLine["reason"], string> = {
  PRODUCTION_PENDING: "waiting on production or surplus",
  PURCHASED_STOCK_SHORT: "purchased stock is short",
  MANUAL_CONFIRMATION_REQUIRED: "needs your manual confirmation",
  PRODUCT_UNAVAILABLE: "product unavailable",
};

const PAYMENT_LABEL: Record<FulfillmentItem["payment_status"], string> = {
  UNPAID: "Unpaid",
  PARTIALLY_PAID: "Partially paid",
  PAID: "Paid",
};

const FULFILLMENT_TIMING_LABEL: Record<FulfillmentItem["timing"], string> = {
  ON_TRACK: "On track",
  AT_RISK: "At risk",
  OVERDUE: "Overdue",
};

export function TodaysFulfillmentsSection({ items }: { items: FulfillmentItem[] }) {
  return (
    <Section id="dash-fulfillments" title="Today's Fulfillments">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Order</TableHead>
            <TableHead>Customer</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Timing</TableHead>
            <TableHead>Payment</TableHead>
            <TableHead>Order total</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((order) => (
            <TableRow key={order.order_id}>
              <TableCell>
                {order.fulfillment_time ? formatTime(order.fulfillment_time) : "No time set"}
              </TableCell>
              <TableCell>
                <Link
                  to={`/app/orders/${order.order_id}`}
                  className="font-medium underline-offset-4 hover:underline"
                >
                  {order.order_number}
                </Link>
              </TableCell>
              <TableCell>{order.customer_name ?? "Guest"}</TableCell>
              <TableCell>
                {order.status === "READY" ? (
                  "Ready"
                ) : (
                  <>
                    Confirmed — not ready
                    {order.unsatisfied_lines.length > 0 && (
                      <ul className="mt-1 text-xs text-muted-foreground">
                        {order.unsatisfied_lines.map((line) => (
                          <li key={line.line_id}>
                            {line.display_name}: {REASON_LABEL[line.reason]}
                          </li>
                        ))}
                      </ul>
                    )}
                  </>
                )}
              </TableCell>
              <TableCell>{FULFILLMENT_TIMING_LABEL[order.timing]}</TableCell>
              <TableCell>{PAYMENT_LABEL[order.payment_status]}</TableCell>
              <TableCell>{formatMoney(order.final_total)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Section>
  );
}

// --------------------------------------------------------------------- Snapshot

export function SnapshotSection({ snapshot }: { snapshot: Snapshot }) {
  const cells: { label: string; value: string }[] = [
    { label: "Orders due today", value: String(snapshot.orders_due_today) },
    { label: "Ready", value: String(snapshot.orders_ready) },
    { label: "Awaiting", value: String(snapshot.orders_awaiting) },
    { label: "At risk", value: String(snapshot.orders_at_risk) },
    { label: "Overdue (all dates)", value: String(snapshot.orders_overdue) },
    {
      label: "Scheduled order value today (not revenue)",
      value: formatMoney(snapshot.scheduled_order_value_today),
    },
    { label: "Production items today", value: String(snapshot.production_items_today) },
    { label: "Planned batches today", value: String(snapshot.planned_batches_today) },
    { label: "Active runs", value: String(snapshot.active_runs) },
  ];
  return (
    <Section id="dash-snapshot" title="Today's Snapshot">
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
        {cells.map((cell) => (
          <div key={cell.label} className="rounded-md border border-border p-2">
            <dt className="text-xs text-muted-foreground">{cell.label}</dt>
            <dd className="text-base font-semibold">{cell.value}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

// ----------------------------------------------------------------------- Upcoming

export function UpcomingSection({ days }: { days: UpcomingDay[] }) {
  return (
    <Section id="dash-upcoming" title="Upcoming">
      <ul className="flex flex-col gap-3">
        {days.map((day) => (
          <li key={day.date} className="rounded-md border border-border p-3 text-sm">
            <div className="flex flex-wrap items-baseline gap-x-3">
              <h3 className="font-semibold">{formatDate(day.date)}</h3>
              <span>
                {day.order_count} {day.order_count === 1 ? "order" : "orders"}
                {day.order_count > 0 && ` · ${formatMoney(day.scheduled_order_value)} scheduled`}
              </span>
              {day.production_item_count > 0 && (
                <span className="text-muted-foreground">
                  {day.production_item_count} to produce · {day.planned_batches}{" "}
                  {day.planned_batches === 1 ? "batch" : "batches"}
                </span>
              )}
            </div>
            {day.orders.length > 0 && (
              <ul className="mt-1 flex flex-col gap-0.5">
                {day.orders.map((order) => (
                  <li key={order.order_id}>
                    <Link
                      to={`/app/orders/${order.order_id}`}
                      className="font-medium underline-offset-4 hover:underline"
                    >
                      {order.order_number}
                    </Link>{" "}
                    · {order.customer_name ?? "Guest"} ·{" "}
                    {order.fulfillment_time ? formatTime(order.fulfillment_time) : "no time set"} ·{" "}
                    {order.status === "READY" ? "Ready" : "Confirmed"}
                  </li>
                ))}
                {day.orders_truncated && (
                  <li className="text-muted-foreground">
                    …and {day.order_count - day.orders.length} more
                  </li>
                )}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}
