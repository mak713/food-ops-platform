// Analytics (Spec §6.14, §12.16, §13; Phase 10): descriptive *historical* business
// intelligence — not the operational Dashboard, not accounting, not forecasting. One global
// period control drives four tabs (Overview, Products, Customers, Orders). Everything is
// derived by the backend from immutable Order snapshots and the historical cost ledger; this
// page only selects a period and renders the server's figures (Spec §12.5).
//
// The selected period, comparison toggle and tab live in the URL so a view is shareable and
// survives a reload.

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { type AnalyticsRange, type PeriodSelection, RANGE_LABELS } from "./api";
import { CustomersTab } from "./CustomersTab";
import { DateRangeControl } from "./DateRangeControl";
import { OrdersTab } from "./OrdersTab";
import { OverviewTab } from "./OverviewTab";
import { ProductsTab } from "./ProductsTab";

type TabId = "overview" | "products" | "customers" | "orders";

const TABS: { id: TabId; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "products", label: "Products" },
  { id: "customers", label: "Customers" },
  { id: "orders", label: "Orders" },
];

function parseRange(value: string | null): AnalyticsRange {
  return value && value in RANGE_LABELS ? (value as AnalyticsRange) : "LAST_30_DAYS";
}

function parseTab(value: string | null): TabId {
  return TABS.some((t) => t.id === value) ? (value as TabId) : "overview";
}

export function AnalyticsPage() {
  const [params, setParams] = useSearchParams();
  const range = parseRange(params.get("range"));
  const selection: PeriodSelection = {
    range,
    startDate: range === "CUSTOM" ? (params.get("start") ?? undefined) : undefined,
    endDate: range === "CUSTOM" ? (params.get("end") ?? undefined) : undefined,
  };
  const tab = parseTab(params.get("tab"));
  // Held in local state (mirrored to the URL): the controlled checkbox must flip instantly,
  // not wait for the router's transition to re-render it from the URL.
  const [compare, setCompare] = useState(params.get("compare") !== "0");

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace: true });
  };

  const selectionKey = JSON.stringify(selection);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Analytics</h1>
        <p className="text-sm text-muted-foreground">
          Historical results from completed Orders and their recorded costs. Recorded Order revenue
          is operational, not accounting or cash revenue. For what to do today, use the Dashboard.
        </p>
      </div>

      <DateRangeControl
        key={selectionKey}
        selection={selection}
        onChange={(next) =>
          update({
            range: next.range,
            start: next.range === "CUSTOM" ? (next.startDate ?? null) : null,
            end: next.range === "CUSTOM" ? (next.endDate ?? null) : null,
          })
        }
      />

      <div role="tablist" aria-label="Analytics sections" className="flex flex-wrap gap-1 border-b">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`analytics-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`analytics-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => update({ tab: t.id })}
            onKeyDown={(e) => {
              const index = TABS.findIndex((x) => x.id === tab);
              if (e.key === "ArrowRight") update({ tab: TABS[(index + 1) % TABS.length].id });
              if (e.key === "ArrowLeft")
                update({ tab: TABS[(index + TABS.length - 1) % TABS.length].id });
            }}
            className={`-mb-px rounded-t-md border-b-2 px-3 py-2 text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50 ${
              tab === t.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`analytics-panel-${tab}`}
        aria-labelledby={`analytics-tab-${tab}`}
        key={`${tab}-${selectionKey}`}
      >
        {tab === "overview" && (
          <OverviewTab
            selection={selection}
            compare={compare}
            onCompareChange={(value) => {
              setCompare(value);
              update({ compare: value ? null : "0" });
            }}
          />
        )}
        {tab === "products" && <ProductsTab selection={selection} />}
        {tab === "customers" && <CustomersTab selection={selection} />}
        {tab === "orders" && <OrdersTab selection={selection} />}
      </div>
    </div>
  );
}
