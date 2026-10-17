// Order analytics (Spec §13.7). Completed Orders are placed in the period by `completed_at`,
// Canceled Orders by `canceled_at`; Canceled Orders appear here only and never in any
// financial figure. "Outstanding recorded balance — as of now" is the currently recorded
// unpaid portion of the period's Completed Orders: payments may still be recorded later, so
// this one figure can legitimately change after the period ends. It is operational, not
// Accounts Receivable.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge } from "../../components/ui/badge";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { formatInstantInTimezone, formatMoney } from "../../lib/format";
import {
  analyticsApi,
  csvUrls,
  type OrderRow,
  type OrderSort,
  type OrderStatusFilter,
  type PeriodSelection,
} from "./api";
import { formatCount } from "./formatting";
import {
  CsvLink,
  EmptyState,
  KpiCard,
  Pager,
  PeriodSummary,
  SectionHeading,
  SelectField,
  AnalyticsError,
} from "./shared";

const PAGE = 25;
const STATUS_OPTIONS: { value: OrderStatusFilter; label: string }[] = [
  { value: "ALL", label: "Completed and Canceled" },
  { value: "COMPLETED", label: "Completed only" },
  { value: "CANCELED", label: "Canceled only" },
];
const SORTS: { value: OrderSort; label: string }[] = [
  { value: "reported_at", label: "Date (newest first)" },
  { value: "final_total", label: "Order total" },
  { value: "outstanding_balance", label: "Outstanding balance" },
];
const METHOD_LABEL: Record<string, string> = {
  PICKUP: "Pickup",
  DELIVERY: "Delivery",
  OTHER: "Other",
  UNSPECIFIED: "Not specified",
};
const PAYMENT_LABEL: Record<string, string> = {
  UNPAID: "Unpaid",
  PARTIALLY_PAID: "Partially paid",
  PAID: "Paid",
};

function OrderTableRow({ row, timezone }: { row: OrderRow; timezone: string }) {
  const canceled = row.status === "CANCELED";
  return (
    <TableRow>
      <TableHead scope="row" className="font-medium">
        <Link to={`/app/orders/${row.order_id}`} className="underline-offset-4 hover:underline">
          {row.order_number}
        </Link>
      </TableHead>
      <TableCell>
        <Badge variant={canceled ? "destructive" : "secondary"}>
          {canceled ? "Canceled" : "Completed"}
        </Badge>
      </TableCell>
      <TableCell>{formatInstantInTimezone(row.reported_at, timezone)}</TableCell>
      <TableCell>{row.customer_name ?? "Guest"}</TableCell>
      <TableCell className="tabular-nums">{formatMoney(row.final_total)}</TableCell>
      <TableCell>{row.payment_status ? PAYMENT_LABEL[row.payment_status] : "—"}</TableCell>
      <TableCell className="tabular-nums">
        {row.outstanding_balance === null ? "—" : formatMoney(row.outstanding_balance)}
      </TableCell>
      <TableCell className="tabular-nums">
        {row.direct_cost === null ? "—" : formatMoney(row.direct_cost)}
      </TableCell>
      <TableCell className="tabular-nums">
        {row.contribution === null ? "—" : formatMoney(row.contribution)}
      </TableCell>
    </TableRow>
  );
}

export function OrdersTab({ selection }: { selection: PeriodSelection }) {
  const [status, setStatus] = useState<OrderStatusFilter>("ALL");
  const [outstandingOnly, setOutstandingOnly] = useState(false);
  const [sort, setSort] = useState<OrderSort>("reported_at");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["analytics", "orders", selection, status, outstandingOnly, sort, offset],
    queryFn: () =>
      analyticsApi.orders(selection, { status, outstandingOnly, sort, limit: PAGE, offset }),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });
  const data = query.data;
  const reset = () => setOffset(0);

  return (
    <section aria-labelledby="orders-heading" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <SectionHeading id="orders-heading">Orders</SectionHeading>
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            id="order-status"
            label="Show"
            value={status}
            options={STATUS_OPTIONS}
            onChange={(v) => {
              setStatus(v);
              reset();
            }}
          />
          <SelectField
            id="order-sort"
            label="Sort orders by"
            value={sort}
            options={SORTS}
            onChange={(v) => {
              setSort(v);
              reset();
            }}
          />
          <label className="flex items-center gap-2 pb-1 text-sm">
            <input
              type="checkbox"
              checked={outstandingOnly}
              onChange={(e) => {
                setOutstandingOnly(e.target.checked);
                reset();
              }}
            />
            Outstanding balance only
          </label>
          <CsvLink
            href={csvUrls.orders(selection, { status, outstandingOnly, sort })}
            label="Download orders CSV"
          />
        </div>
      </div>
      {data && <PeriodSummary header={data} />}
      {query.isPending && <p className="text-sm text-muted-foreground">Loading orders…</p>}
      {query.isError && (
        <AnalyticsError
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="Could not load Order analytics."
        />
      )}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <KpiCard
              testId="kpi-completed"
              label="Completed Orders"
              value={formatCount(data.summary.completed_orders)}
              caption={`Revenue ${formatMoney(data.summary.revenue)}`}
            />
            <KpiCard
              testId="kpi-canceled"
              label="Canceled Orders"
              value={formatCount(data.summary.canceled_orders)}
              caption="Counted by cancellation date; never part of Revenue"
            />
            <KpiCard
              testId="kpi-largest"
              label="Largest completed Order"
              value={
                data.summary.largest_completed_order
                  ? formatMoney(data.summary.largest_completed_order.final_total)
                  : "—"
              }
              caption={
                data.summary.largest_completed_order
                  ? `${data.summary.largest_completed_order.order_number} · ${
                      data.summary.largest_completed_order.customer_name ?? "Guest"
                    }`
                  : "No Completed Orders"
              }
            />
            <KpiCard
              testId="kpi-outstanding"
              label="Outstanding recorded balance — as of now"
              value={formatMoney(data.summary.outstanding.balance)}
              caption={`${formatCount(data.summary.outstanding.order_count)} completed ${
                data.summary.outstanding.order_count === 1 ? "Order" : "Orders"
              } with a balance`}
            />
          </div>

          <Table className="caption-top">
            <TableCaption className="mb-2 text-left font-medium text-foreground">
              Completed Orders by fulfillment method
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Method</TableHead>
                <TableHead scope="col">Completed Orders</TableHead>
                <TableHead scope="col">Revenue</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.summary.fulfillment_methods.map((m) => (
                <TableRow key={m.method}>
                  <TableHead scope="row" className="font-medium">
                    {METHOD_LABEL[m.method] ?? m.method}
                  </TableHead>
                  <TableCell className="tabular-nums">{formatCount(m.completed_orders)}</TableCell>
                  <TableCell className="tabular-nums">{formatMoney(m.revenue)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {data.items.length === 0 ? (
            <EmptyState>No matching Orders in this period.</EmptyState>
          ) : (
            <Table className="caption-top">
              <TableCaption className="mb-2 text-left font-medium text-foreground">
                Orders completed or canceled in the selected period
              </TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Order</TableHead>
                  <TableHead scope="col">Status</TableHead>
                  <TableHead scope="col">Completed / canceled</TableHead>
                  <TableHead scope="col">Customer</TableHead>
                  <TableHead scope="col">Total</TableHead>
                  <TableHead scope="col">Payment</TableHead>
                  <TableHead scope="col">Balance now</TableHead>
                  <TableHead scope="col">Direct Cost (internal)</TableHead>
                  <TableHead scope="col">Contribution (internal)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((row) => (
                  <OrderTableRow key={row.order_id} row={row} timezone={data.timezone} />
                ))}
              </TableBody>
            </Table>
          )}
          <Pager
            total={data.total}
            limit={data.limit}
            offset={data.offset}
            onOffset={setOffset}
            noun="orders"
          />
        </>
      )}
    </section>
  );
}
