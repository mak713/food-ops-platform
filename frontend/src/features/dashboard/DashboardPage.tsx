// Operational Dashboard (Spec §12.15, DASH-001..005; ADR-069). Priority order:
//   1. Attention Required  2. Active Production  3. Today's Production
//   4. Today's Fulfillments  5. Today's Snapshot  6. Upcoming
// Empty, irrelevant sections are not rendered (DASH-002). The page owns no business truth:
// it renders what GET /api/v1/dashboard derives from authoritative operational state, and
// refetches on mount and on window focus so it reflects writes made elsewhere (AC-DASH-005).

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { buttonVariants } from "../../components/ui/button";
import { formatDate } from "../../lib/format";
import { dashboardApi, type Dashboard } from "./api";
import {
  ActiveProductionSection,
  AttentionSection,
  SnapshotSection,
  TodaysFulfillmentsSection,
  TodaysProductionSection,
  UpcomingSection,
} from "./DashboardSections";

function DashboardContent({ dashboard }: { dashboard: Dashboard }) {
  const production = dashboard.todays_production;
  const snapshot = dashboard.todays_snapshot;

  const hasAttention = dashboard.attention.length > 0;
  const hasActive = dashboard.active_production.length > 0;
  const hasProduction = production.items.length > 0 || production.manual_workload !== null;
  const hasFulfillments = dashboard.todays_fulfillments.length > 0;
  const hasSnapshot =
    snapshot.orders_due_today > 0 ||
    snapshot.orders_overdue > 0 ||
    snapshot.production_items_today > 0 ||
    snapshot.active_runs > 0;
  const hasUpcoming = dashboard.upcoming.length > 0;
  const nothingToShow =
    !hasAttention &&
    !hasActive &&
    !hasProduction &&
    !hasFulfillments &&
    !hasSnapshot &&
    !hasUpcoming;

  return (
    <>
      <p className="text-sm text-muted-foreground">
        {formatDate(dashboard.business_today)} · {dashboard.timezone}
      </p>
      {nothingToShow && (
        <p className="text-sm" data-testid="dashboard-empty">
          Nothing needs your attention and nothing is scheduled. Confirmed orders will appear here.
        </p>
      )}
      {hasAttention && <AttentionSection alerts={dashboard.attention} />}
      {hasActive && (
        <ActiveProductionSection runs={dashboard.active_production} timezone={dashboard.timezone} />
      )}
      {hasProduction && (
        <TodaysProductionSection
          items={production.items}
          manualWorkload={production.manual_workload}
          timezone={dashboard.timezone}
        />
      )}
      {hasFulfillments && <TodaysFulfillmentsSection items={dashboard.todays_fulfillments} />}
      {hasSnapshot && <SnapshotSection snapshot={snapshot} />}
      {hasUpcoming && <UpcomingSection days={dashboard.upcoming} />}
    </>
  );
}

export function DashboardPage() {
  const query = useQuery({
    queryKey: ["dashboard"],
    queryFn: dashboardApi.get,
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });

  const dashboard = query.data;
  // A payload that isn't the expected shape (contract drift, an intermediary's reply) is
  // surfaced as an error rather than crashing the page or silently rendering nothing.
  const isWellFormed =
    dashboard !== undefined &&
    Array.isArray(dashboard.attention) &&
    Array.isArray(dashboard.upcoming) &&
    dashboard.todays_production !== undefined &&
    dashboard.todays_snapshot !== undefined;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <div className="flex gap-2">
          <Link
            to="/app/production/shopping-list"
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            Shopping List
          </Link>
          <Link to="/app/production" className={buttonVariants({ variant: "outline", size: "sm" })}>
            Production Plan
          </Link>
        </div>
      </div>

      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : query.isError ? (
        <ErrorBanner error={query.error} onRetry={() => query.refetch()} />
      ) : isWellFormed ? (
        <DashboardContent dashboard={dashboard} />
      ) : (
        <ErrorBanner
          error={null}
          onRetry={() => query.refetch()}
          fallbackMessage="The dashboard response was not in the expected format."
        />
      )}
    </div>
  );
}
