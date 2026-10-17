// Product analytics (Spec §13.5, ANA-009). Underlying Product units and Selling-Option
// package counts are separate columns and are never added together. "Most profitable" is the
// highest total Contribution; "Highest margin" is the highest Contribution Margin — two
// different metrics, labeled and sortable separately.
//
// Product Revenue is the immutable Order Line subtotal. Order-level adjustments, manual tax,
// Custom Items and any unattributed cost are shown as explicit reconciliation rows, never
// apportioned across Products, so the table reconciles to the Overview (Q1).

import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { formatMoney } from "../../lib/format";
import {
  analyticsApi,
  csvUrls,
  type PeriodSelection,
  type ProductSort,
  type Products,
} from "./api";
import { formatCount, formatRatioAsPercent, INTERNAL_NOTE } from "./formatting";
import {
  CsvLink,
  EmptyState,
  PeriodSummary,
  SectionHeading,
  SelectField,
  AnalyticsError,
} from "./shared";

const SORTS: { value: ProductSort; label: string }[] = [
  { value: "contribution", label: "Most profitable (total Contribution)" },
  { value: "margin", label: "Highest margin (Contribution Margin)" },
  { value: "revenue", label: "Revenue" },
  { value: "units", label: "Units sold" },
  { value: "orders", label: "Completed Orders" },
  { value: "name", label: "Name (A–Z)" },
];

function Highlights({ data }: { data: Products }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div className="rounded-lg border border-border p-3" data-testid="highlight-profitable">
        <div className="text-xs font-medium text-muted-foreground">
          Most profitable — highest total Contribution
        </div>
        {data.most_profitable ? (
          <div className="text-base font-semibold">
            {data.most_profitable.product_name}{" "}
            <span className="tabular-nums">{formatMoney(data.most_profitable.value)}</span>
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">—</div>
        )}
      </div>
      <div className="rounded-lg border border-border p-3" data-testid="highlight-margin">
        <div className="text-xs font-medium text-muted-foreground">
          Highest margin — Contribution ÷ Revenue
        </div>
        {data.highest_margin ? (
          <div className="text-base font-semibold">
            {data.highest_margin.product_name}{" "}
            <span className="tabular-nums">{formatRatioAsPercent(data.highest_margin.value)}</span>
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">—</div>
        )}
      </div>
    </div>
  );
}

function ProductRows({ data }: { data: Products }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  return (
    <>
      {data.rows.map((row) => {
        const expanded = open[row.product_id] ?? false;
        return (
          <Fragment key={row.product_id}>
            <TableRow>
              <TableHead scope="row" className="font-medium">
                {row.product_name}
                {row.is_active === false && (
                  <Badge variant="outline" className="ml-2">
                    Archived
                  </Badge>
                )}
              </TableHead>
              <TableCell className="tabular-nums">
                {formatQuantityForDisplay(row.underlying_units)}
              </TableCell>
              <TableCell className="tabular-nums">{formatMoney(row.revenue)}</TableCell>
              <TableCell className="tabular-nums">{formatMoney(row.direct_cost)}</TableCell>
              <TableCell className="tabular-nums">{formatMoney(row.contribution)}</TableCell>
              <TableCell className="tabular-nums">
                {formatRatioAsPercent(row.contribution_margin)}
              </TableCell>
              <TableCell className="tabular-nums">{formatCount(row.completed_orders)}</TableCell>
              <TableCell>
                <Button
                  variant="outline"
                  size="sm"
                  aria-expanded={expanded}
                  aria-label={`${expanded ? "Hide" : "Show"} selling options for ${row.product_name}`}
                  onClick={() => setOpen((v) => ({ ...v, [row.product_id]: !expanded }))}
                >
                  {expanded ? "Hide options" : "Selling options"}
                </Button>
              </TableCell>
            </TableRow>
            {expanded &&
              row.selling_options.map((o) => (
                <TableRow
                  key={`${row.product_id}-${o.selling_option_id ?? "custom"}`}
                  className="bg-muted/30"
                  data-testid={`option-${row.product_id}`}
                >
                  <TableHead scope="row" className="pl-6 font-normal">
                    {o.name}
                    <span className="ml-2 text-xs text-muted-foreground">
                      {o.package_count === null
                        ? "units only — no package count"
                        : `${formatQuantityForDisplay(o.package_count)} packages`}
                    </span>
                  </TableHead>
                  <TableCell className="tabular-nums">
                    {formatQuantityForDisplay(o.underlying_units)}
                  </TableCell>
                  <TableCell className="tabular-nums">{formatMoney(o.revenue)}</TableCell>
                  <TableCell className="tabular-nums">{formatMoney(o.direct_cost)}</TableCell>
                  <TableCell className="tabular-nums">{formatMoney(o.contribution)}</TableCell>
                  <TableCell className="tabular-nums">
                    {formatRatioAsPercent(o.contribution_margin)}
                  </TableCell>
                  <TableCell className="tabular-nums">{formatCount(o.completed_orders)}</TableCell>
                  <TableCell />
                </TableRow>
              ))}
          </Fragment>
        );
      })}
    </>
  );
}

export function ProductsTab({ selection }: { selection: PeriodSelection }) {
  const [sort, setSort] = useState<ProductSort>("contribution");
  const query = useQuery({
    queryKey: ["analytics", "products", selection, sort],
    queryFn: () => analyticsApi.products(selection, { sort }),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });
  const data = query.data;

  return (
    <section aria-labelledby="products-heading" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <SectionHeading id="products-heading">Products</SectionHeading>
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            id="product-sort"
            label="Sort products by"
            value={sort}
            options={SORTS}
            onChange={setSort}
          />
          <CsvLink href={csvUrls.products(selection, { sort })} label="Download products CSV" />
        </div>
      </div>
      {data && <PeriodSummary header={data} />}
      <p className="text-xs text-muted-foreground">
        Product Revenue is the Order Line price snapshot. Units are underlying Product units;
        packages are Selling-Option counts and are shown separately. A multi-Product Order counts
        once under each Product. {INTERNAL_NOTE}
      </p>
      {query.isPending && <p className="text-sm text-muted-foreground">Loading products…</p>}
      {query.isError && (
        <AnalyticsError
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="Could not load Product analytics."
        />
      )}
      {data && (
        <>
          <Highlights data={data} />
          {data.rows.length === 0 && !data.custom_items ? (
            <EmptyState>No Product sales were completed in this period.</EmptyState>
          ) : (
            <Table className="caption-top">
              <TableCaption className="mb-2 text-left font-medium text-foreground">
                Product results for the selected period
              </TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Product</TableHead>
                  <TableHead scope="col">Units sold</TableHead>
                  <TableHead scope="col">Revenue</TableHead>
                  <TableHead scope="col">Direct Cost</TableHead>
                  <TableHead scope="col">Contribution</TableHead>
                  <TableHead scope="col">Margin</TableHead>
                  <TableHead scope="col">Completed Orders</TableHead>
                  <TableHead scope="col">
                    <span className="sr-only">Selling options</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <ProductRows data={data} />
                <TableRow className="border-t-2">
                  <TableHead colSpan={8} scope="colgroup" className="text-xs text-muted-foreground">
                    Not attributed to a Product (shown so the table reconciles to the Overview)
                  </TableHead>
                </TableRow>
                {data.custom_items && (
                  <TableRow data-testid="row-custom-items">
                    <TableHead scope="row" className="font-medium">
                      Custom items (no Product)
                    </TableHead>
                    <TableCell>—</TableCell>
                    <TableCell className="tabular-nums">
                      {formatMoney(data.custom_items.revenue)}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {formatMoney(data.custom_items.direct_cost)}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {formatMoney(data.custom_items.contribution)}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {formatRatioAsPercent(data.custom_items.contribution_margin)}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {formatCount(data.custom_items.completed_orders)}
                    </TableCell>
                    <TableCell />
                  </TableRow>
                )}
                <TableRow data-testid="row-adjustments">
                  <TableHead scope="row" className="font-medium">
                    Order adjustments
                  </TableHead>
                  <TableCell>—</TableCell>
                  <TableCell className="tabular-nums">
                    {formatMoney(data.order_level.order_adjustments)}
                  </TableCell>
                  <TableCell colSpan={5} />
                </TableRow>
                <TableRow data-testid="row-tax">
                  <TableHead scope="row" className="font-medium">
                    Manual tax
                  </TableHead>
                  <TableCell>—</TableCell>
                  <TableCell className="tabular-nums">
                    {formatMoney(data.order_level.manual_tax)}
                  </TableCell>
                  <TableCell colSpan={5} />
                </TableRow>
                <TableRow data-testid="row-unattributed">
                  <TableHead scope="row" className="font-medium">
                    Unattributed Direct Cost
                  </TableHead>
                  <TableCell>—</TableCell>
                  <TableCell />
                  <TableCell className="tabular-nums">
                    {formatMoney(data.order_level.unattributed_direct_cost)}
                  </TableCell>
                  <TableCell colSpan={4} />
                </TableRow>
              </TableBody>
              <TableFooter>
                <TableRow data-testid="row-totals">
                  <TableHead scope="row" className="font-semibold">
                    All completed Orders
                  </TableHead>
                  <TableCell />
                  <TableCell className="tabular-nums">{formatMoney(data.totals.revenue)}</TableCell>
                  <TableCell className="tabular-nums">
                    {formatMoney(data.totals.direct_cost)}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {formatMoney(data.totals.contribution)}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {formatRatioAsPercent(data.totals.contribution_margin)}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {formatCount(data.totals.completed_orders)}
                  </TableCell>
                  <TableCell />
                </TableRow>
              </TableFooter>
            </Table>
          )}
          {Number(data.order_level.unreconciled_revenue) !== 0 && (
            <p role="status" className="text-xs text-destructive">
              Revenue not explained by line subtotals, adjustments and tax:{" "}
              {formatMoney(data.order_level.unreconciled_revenue)}
            </p>
          )}
        </>
      )}
    </section>
  );
}
