// Analytics Overview (Spec §13.3–13.4). KPI cards with an *optional previous-period
// comparison that applies to these KPI cards only* (the trend, Products, Customers and Orders
// always report the selected period), plus the Revenue / Contribution trend.
//
// Wording: Revenue is "recorded Order revenue" (final totals of Completed Orders — operational,
// not accounting). Direct Cost / Contribution / Margin are grouped as internal metrics
// (ORD-014, AC-UX-004).

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { formatMoney } from "../../lib/format";
import {
  analyticsApi,
  csvUrls,
  type Kpis,
  type MoneyDelta,
  type Overview,
  type PeriodSelection,
} from "./api";
import {
  formatCount,
  formatPercentagePoints,
  formatRatioAsPercent,
  formatSignedCount,
  formatSignedMoney,
  formatSignedPercent,
  INTERNAL_NOTE,
  describePeriod,
} from "./formatting";
import {
  CsvLink,
  EmptyState,
  KpiCard,
  PeriodSummary,
  SectionHeading,
  AnalyticsError,
} from "./shared";
import { TrendChart } from "./TrendChart";

interface Props {
  selection: PeriodSelection;
  compare: boolean;
  onCompareChange: (compare: boolean) => void;
}

function moneyDeltaText(delta: MoneyDelta): string | null {
  if (delta.absolute === null) return null;
  const percent =
    delta.percent_change !== null ? ` (${formatSignedPercent(delta.percent_change)})` : "";
  return `${formatSignedMoney(delta.absolute)}${percent}`;
}

function Delta({ text, noPrior }: { text: string | null; noPrior: boolean }) {
  if (noPrior) {
    return <span className="text-muted-foreground">No prior-period completed-order data</span>;
  }
  if (text === null) {
    return <span className="text-muted-foreground">No comparison available</span>;
  }
  return (
    <span>
      <span className="font-medium">{text}</span>{" "}
      <span className="text-muted-foreground">vs previous period</span>
    </span>
  );
}

function Cards({ data, compare }: { data: Overview; compare: boolean }) {
  const cur: Kpis = data.current;
  const deltas = compare ? data.deltas : null;
  const noPrior = compare && data.previous_has_data === false;
  const d = (node: string | null): ReactNode =>
    deltas ? <Delta text={node} noPrior={noPrior} /> : undefined;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          testId="kpi-revenue"
          label="Revenue (recorded Order revenue)"
          value={formatMoney(cur.revenue)}
          caption="Final totals of Completed Orders"
          delta={d(deltas ? moneyDeltaText(deltas.revenue) : null)}
        />
        <KpiCard
          testId="kpi-orders"
          label="Completed Orders"
          value={formatCount(cur.completed_orders)}
          delta={d(
            deltas
              ? `${formatSignedCount(deltas.completed_orders.absolute)}${
                  deltas.completed_orders.percent_change !== null
                    ? ` (${formatSignedPercent(deltas.completed_orders.percent_change)})`
                    : ""
                }`
              : null,
          )}
        />
        <KpiCard
          testId="kpi-aov"
          label="Average Order Value"
          value={cur.average_order_value === null ? "—" : formatMoney(cur.average_order_value)}
          caption={cur.average_order_value === null ? "No Completed Orders" : "Revenue ÷ Orders"}
          delta={d(deltas ? moneyDeltaText(deltas.average_order_value) : null)}
        />
      </div>

      <section
        aria-labelledby="internal-metrics"
        className="flex flex-col gap-2 rounded-lg border border-dashed border-border p-3"
      >
        <div>
          <h3 id="internal-metrics" className="text-sm font-semibold">
            Internal metrics
          </h3>
          <p className="text-xs text-muted-foreground">{INTERNAL_NOTE}</p>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <KpiCard
            internal
            testId="kpi-direct-cost"
            label="Direct Cost"
            value={formatMoney(cur.direct_cost)}
            caption="Historical cost of those Orders"
            delta={d(deltas ? moneyDeltaText(deltas.direct_cost) : null)}
          />
          <KpiCard
            internal
            testId="kpi-contribution"
            label="Contribution"
            value={formatMoney(cur.contribution)}
            caption="Revenue − Direct Cost"
            delta={d(deltas ? moneyDeltaText(deltas.contribution) : null)}
          />
          <KpiCard
            internal
            testId="kpi-margin"
            label="Contribution Margin"
            value={formatRatioAsPercent(cur.contribution_margin)}
            caption={
              cur.contribution_margin === null
                ? "Not defined with zero Revenue"
                : "Contribution ÷ Revenue"
            }
            delta={d(
              deltas && deltas.contribution_margin.absolute !== null
                ? formatPercentagePoints(deltas.contribution_margin.absolute)
                : null,
            )}
          />
        </div>
      </section>
    </div>
  );
}

export function OverviewTab({ selection, compare, onCompareChange }: Props) {
  const overview = useQuery({
    queryKey: ["analytics", "overview", selection, compare],
    queryFn: () => analyticsApi.overview(selection, compare),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });
  const trends = useQuery({
    queryKey: ["analytics", "trends", selection],
    queryFn: () => analyticsApi.trends(selection),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });

  return (
    <div className="flex flex-col gap-6">
      <section aria-labelledby="overview-kpis" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionHeading id="overview-kpis">Overview</SectionHeading>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={compare}
              onChange={(e) => onCompareChange(e.target.checked)}
            />
            Compare overview KPIs to previous period
          </label>
        </div>
        {overview.data && <PeriodSummary header={overview.data} />}
        {compare && overview.data?.comparison_period && (
          <p className="text-xs text-muted-foreground" data-testid="comparison-note">
            Previous period: {describePeriod(overview.data.comparison_period)} — the same number of
            calendar days immediately before the selected period. Comparison applies to the KPI
            cards only; the chart and the other tabs show the selected period.
          </p>
        )}
        {overview.isPending && <p className="text-sm text-muted-foreground">Loading overview…</p>}
        {overview.isError && (
          <AnalyticsError
            error={overview.error}
            onRetry={() => overview.refetch()}
            fallbackMessage="Could not load the Analytics overview."
          />
        )}
        {overview.data && <Cards data={overview.data} compare={compare} />}
        {overview.data && overview.data.current.completed_orders === 0 && (
          <EmptyState>No Orders were completed in this period.</EmptyState>
        )}
      </section>

      <section aria-labelledby="overview-trend" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionHeading id="overview-trend">Trend</SectionHeading>
          <CsvLink href={csvUrls.trends(selection)} label="Download trend CSV" />
        </div>
        {trends.isPending && <p className="text-sm text-muted-foreground">Loading trend…</p>}
        {trends.isError && !overview.isError && (
          <AnalyticsError
            error={trends.error}
            onRetry={() => trends.refetch()}
            fallbackMessage="Could not load the trend."
          />
        )}
        {trends.data && <TrendChart trends={trends.data} />}
      </section>
    </div>
  );
}
