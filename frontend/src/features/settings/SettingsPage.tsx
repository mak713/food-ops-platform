// Settings (Spec §6.15, §12.17; Phase 11): one canonical page with four areas — Business
// Profile, Costing & Pricing, Operations, Account. The active area lives in the URL
// (`?area=`) so a view is linkable and survives a reload; the former `/app/account` route
// redirects to `?area=account`.
//
// Each panel carries a static heading so it renders without waiting for any data. The
// Business Settings query is only enabled while a Business area is showing: the Account area
// works entirely from the authenticated `me` and needs no Settings request.

import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { AccountArea } from "./AccountArea";
import { settingsApi, SETTINGS_QUERY_KEY, type BusinessSettings } from "./api";
import { BusinessProfileForm } from "./BusinessProfileForm";
import { CostingPricingForm } from "./CostingPricingForm";
import { OperationsForm } from "./OperationsForm";

type AreaId = "profile" | "costing" | "operations" | "account";

const AREAS: { id: AreaId; label: string }[] = [
  { id: "profile", label: "Business Profile" },
  { id: "costing", label: "Costing & Pricing" },
  { id: "operations", label: "Operations" },
  { id: "account", label: "Account" },
];

function parseArea(value: string | null): AreaId {
  return AREAS.some((a) => a.id === value) ? (value as AreaId) : "profile";
}

export function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const area = parseArea(params.get("area"));
  const current = AREAS.find((a) => a.id === area) ?? AREAS[0];

  const select = (next: AreaId) => {
    const nextParams = new URLSearchParams(params);
    nextParams.set("area", next);
    setParams(nextParams, { replace: true });
  };

  const query = useQuery<BusinessSettings>({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: settingsApi.get,
    enabled: area !== "account",
    retry: false,
    refetchOnWindowFocus: false,
  });

  const refresh = async () => (await query.refetch()).data;

  const businessArea = () => {
    if (query.isError) {
      return (
        <ErrorBanner
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="We couldn't load your settings. Please try again."
        />
      );
    }
    if (!query.data) {
      return <p className="text-sm text-muted-foreground">Loading settings…</p>;
    }
    const props = { initial: query.data, refresh };
    if (area === "profile") return <BusinessProfileForm {...props} />;
    if (area === "costing") return <CostingPricingForm {...props} />;
    return <OperationsForm {...props} />;
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Current business settings. Changing them affects future work and how data is shown; saved
          history is never rewritten.
        </p>
      </div>

      <div role="tablist" aria-label="Settings areas" className="flex flex-wrap gap-1 border-b">
        {AREAS.map((a) => (
          <button
            key={a.id}
            type="button"
            role="tab"
            id={`settings-tab-${a.id}`}
            aria-selected={area === a.id}
            aria-controls={`settings-panel-${a.id}`}
            tabIndex={area === a.id ? 0 : -1}
            onClick={() => select(a.id)}
            onKeyDown={(e) => {
              const index = AREAS.findIndex((x) => x.id === area);
              let target: number | null = null;
              if (e.key === "ArrowRight") target = (index + 1) % AREAS.length;
              if (e.key === "ArrowLeft") target = (index + AREAS.length - 1) % AREAS.length;
              if (e.key === "Home") target = 0;
              if (e.key === "End") target = AREAS.length - 1;
              if (target !== null) {
                e.preventDefault();
                select(AREAS[target].id);
                document.getElementById(`settings-tab-${AREAS[target].id}`)?.focus();
              }
            }}
            className={`-mb-px rounded-t-md border-b-2 px-3 py-2 text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50 ${
              area === a.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {a.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`settings-panel-${area}`}
        aria-labelledby={`settings-tab-${area}`}
        className="flex flex-col gap-4"
      >
        <h2 className="text-lg font-semibold">{current.label}</h2>
        {area === "account" ? <AccountArea /> : businessArea()}
      </div>
    </div>
  );
}
