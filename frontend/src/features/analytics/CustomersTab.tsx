// Customer analytics (Spec §13.6, ANA-007). Only Completed Orders count. New = no earlier
// Completed Order before the period; Returning = at least one. Guest (walk-in) Orders are
// reported separately and never appear as a Customer row. No churn / loyalty / profitability.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
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
import { formatMoney } from "../../lib/format";
import { analyticsApi, csvUrls, type CustomerSort, type PeriodSelection } from "./api";
import { formatCount, formatRatioAsPercent } from "./formatting";
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
const SORTS: { value: CustomerSort; label: string }[] = [
  { value: "revenue", label: "Top by Revenue" },
  { value: "completed_orders", label: "Top by Completed Orders" },
  { value: "name", label: "Name (A–Z)" },
];

export function CustomersTab({ selection }: { selection: PeriodSelection }) {
  const [sort, setSort] = useState<CustomerSort>("revenue");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["analytics", "customers", selection, sort, offset],
    queryFn: () => analyticsApi.customers(selection, { sort, limit: PAGE, offset }),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });
  const data = query.data;
  const guest = data?.summary.guest;

  return (
    <section aria-labelledby="customers-heading" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <SectionHeading id="customers-heading">Customers</SectionHeading>
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            id="customer-sort"
            label="Sort customers by"
            value={sort}
            options={SORTS}
            onChange={(value) => {
              setSort(value);
              setOffset(0);
            }}
          />
          <CsvLink href={csvUrls.customers(selection, { sort })} label="Download customers CSV" />
        </div>
      </div>
      {data && <PeriodSummary header={data} />}
      {query.isPending && <p className="text-sm text-muted-foreground">Loading customers…</p>}
      {query.isError && (
        <AnalyticsError
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="Could not load Customer analytics."
        />
      )}
      {data && guest && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <KpiCard
              testId="kpi-unique-customers"
              label="Unique customers (non-Guest)"
              value={formatCount(data.summary.unique_customers)}
            />
            <KpiCard
              testId="kpi-new-customers"
              label="New customers"
              value={formatCount(data.summary.new_customers)}
              caption="First Completed Order is in this period"
            />
            <KpiCard
              testId="kpi-returning-customers"
              label="Returning customers"
              value={formatCount(data.summary.returning_customers)}
              caption="Completed an Order before this period"
            />
            <KpiCard
              testId="kpi-guest"
              label="Guest orders"
              value={formatCount(guest.completed_orders)}
              caption={`${formatMoney(guest.revenue)} Revenue · AOV ${
                guest.average_order_value === null ? "—" : formatMoney(guest.average_order_value)
              } · ${formatRatioAsPercent(guest.share_of_completed_orders)} of completed Orders`}
            />
          </div>
          {data.items.length === 0 ? (
            <EmptyState>No customers completed an Order in this period.</EmptyState>
          ) : (
            <Table className="caption-top">
              <TableCaption className="mb-2 text-left font-medium text-foreground">
                Customers with a Completed Order in the selected period
              </TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Customer</TableHead>
                  <TableHead scope="col">Status</TableHead>
                  <TableHead scope="col">Completed Orders</TableHead>
                  <TableHead scope="col">Revenue</TableHead>
                  <TableHead scope="col">Average Order Value</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((c) => (
                  <TableRow key={c.customer_id}>
                    <TableHead scope="row" className="font-medium">
                      {c.customer_name}
                      {c.is_active === false && (
                        <Badge variant="outline" className="ml-2">
                          Archived
                        </Badge>
                      )}
                    </TableHead>
                    <TableCell>
                      <Badge variant={c.classification === "NEW" ? "secondary" : "outline"}>
                        {c.classification === "NEW" ? "New" : "Returning"}
                      </Badge>
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {formatCount(c.completed_orders)}
                    </TableCell>
                    <TableCell className="tabular-nums">{formatMoney(c.revenue)}</TableCell>
                    <TableCell className="tabular-nums">
                      {c.average_order_value === null ? "—" : formatMoney(c.average_order_value)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <Pager
            total={data.total}
            limit={data.limit}
            offset={data.offset}
            onOffset={setOffset}
            noun="customers"
          />
        </>
      )}
    </section>
  );
}
