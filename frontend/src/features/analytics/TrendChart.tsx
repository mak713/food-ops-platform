// Revenue / Direct Cost / Contribution trend (Spec §13.4, §12.16: Recharts). The chart is a
// visual aid only; the same server figures are always shown beside it as a readable table
// (accessibility: the chart is never the only representation of the data). `Number()` is used
// solely to place points on the chart's pixel scale — displayed values come from the server
// strings.

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
import { formatMoney } from "../../lib/format";
import type { Trends } from "./api";
import { formatCount, formatRatioAsPercent } from "./formatting";

const GRANULARITY_LABEL = { DAY: "daily", WEEK: "weekly (weeks start Monday)", MONTH: "monthly" };

function axisMoney(value: number): string {
  return `$${Math.round(value).toLocaleString("en-US")}`;
}

export function TrendChart({ trends }: { trends: Trends }) {
  const data = trends.buckets.map((b) => ({
    label: b.label,
    revenue: Number(b.revenue),
    directCost: Number(b.direct_cost),
    contribution: Number(b.contribution),
  }));
  const granularity = GRANULARITY_LABEL[trends.granularity];

  return (
    <div className="flex flex-col gap-4">
      <figure
        className="m-0"
        aria-label={`Revenue, Direct Cost and Contribution, ${granularity}`}
        data-testid="trend-chart"
      >
        <div className="h-72 w-full">
          <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={0}>
            <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="label" tick={{ fontSize: 12 }} />
              <YAxis tickFormatter={axisMoney} width={72} tick={{ fontSize: 12 }} />
              <Tooltip formatter={(value) => formatMoney(Number(value).toFixed(2))} />
              <Legend />
              <Line
                type="linear"
                dataKey="revenue"
                name="Revenue"
                stroke="#2563eb"
                strokeWidth={2}
                dot={data.length <= 31}
                isAnimationActive={false}
              />
              <Line
                type="linear"
                dataKey="contribution"
                name="Contribution"
                stroke="#16a34a"
                strokeWidth={2}
                dot={data.length <= 31}
                isAnimationActive={false}
              />
              <Line
                type="linear"
                dataKey="directCost"
                name="Direct Cost"
                stroke="#dc2626"
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <figcaption className="text-xs text-muted-foreground">
          Trend by {granularity} bucket in the Business timezone. Contribution = Revenue − Direct
          Cost. The table below lists the same values.
        </figcaption>
      </figure>

      <Table className="caption-top">
        <TableCaption className="mb-2 text-left text-sm font-medium text-foreground">
          Trend data ({granularity})
        </TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead scope="col">Period</TableHead>
            <TableHead scope="col">Revenue</TableHead>
            <TableHead scope="col">Direct Cost</TableHead>
            <TableHead scope="col">Contribution</TableHead>
            <TableHead scope="col">Completed Orders</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {trends.buckets.map((b) => (
            <TableRow key={b.index}>
              <TableHead scope="row" className="font-medium text-foreground">
                {b.label}
                {b.start_date !== b.end_date && (
                  <span className="ml-1 text-xs text-muted-foreground">
                    ({b.start_date} – {b.end_date})
                  </span>
                )}
              </TableHead>
              <TableCell className="tabular-nums">{formatMoney(b.revenue)}</TableCell>
              <TableCell className="tabular-nums">{formatMoney(b.direct_cost)}</TableCell>
              <TableCell className="tabular-nums">{formatMoney(b.contribution)}</TableCell>
              <TableCell className="tabular-nums">{formatCount(b.completed_orders)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
        <TableFooter>
          <TableRow>
            <TableHead scope="row" className="font-semibold text-foreground">
              Total
            </TableHead>
            <TableCell className="tabular-nums">{formatMoney(trends.totals.revenue)}</TableCell>
            <TableCell className="tabular-nums">{formatMoney(trends.totals.direct_cost)}</TableCell>
            <TableCell className="tabular-nums">
              {formatMoney(trends.totals.contribution)}
              {trends.totals.contribution_margin !== null && (
                <span className="ml-1 text-xs text-muted-foreground">
                  ({formatRatioAsPercent(trends.totals.contribution_margin)} margin)
                </span>
              )}
            </TableCell>
            <TableCell className="tabular-nums">
              {formatCount(trends.totals.completed_orders)}
            </TableCell>
          </TableRow>
        </TableFooter>
      </Table>
    </div>
  );
}
