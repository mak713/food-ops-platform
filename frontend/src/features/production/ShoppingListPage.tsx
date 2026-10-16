// Shopping List (Spec §12.12, PLAN-011; Phase 9). Shortage-first, derived entirely by the
// backend from active demand and current inventory — nothing here is maintained by hand
// and no shortage math happens in React (Spec §12.5). Horizon presets (3 / 7 days) and a
// custom range; each shortage expands into the contributing demand (requirements and the
// Orders that feed them).
//
// Freshness (AC-PLAN-010): every write that changes demand or inventory happens on another
// page, so the query always refetches on mount and on window focus instead of every
// mutation site having to know about this screen.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { Button, buttonVariants } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
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
import {
  shoppingListApi,
  type ShoppingListContributor,
  type ShoppingListItem,
  type ShoppingListSelection,
} from "./shoppingListApi";

function qty(value: string, unit: string): string {
  return `${formatQuantityForDisplay(value)} ${unit}`;
}

function Contributors({ item }: { item: ShoppingListItem }) {
  // Show the cumulative shortage only where it changes — a long custom range would
  // otherwise print one near-identical row per day. Display-only filtering.
  const changePoints = item.by_date.filter((entry, index) => {
    const previous = item.by_date[index - 1];
    return (
      index === item.by_date.length - 1 ||
      !previous ||
      entry.cumulative_reserved_through_date !== previous.cumulative_reserved_through_date
    );
  });

  return (
    <div className="flex flex-col gap-4 p-3" data-testid={`drilldown-${item.ingredient_id}`}>
      <div>
        <h3 className="text-sm font-semibold">Contributing demand</h3>
        <p className="mb-2 text-xs text-muted-foreground">
          Reserved quantities per production requirement. Stock is shared across earlier demand, so
          the shortage builds up date by date.
        </p>
        {item.contributors.length === 0 ? (
          <p className="text-sm text-muted-foreground">No contributing demand in this range.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {item.contributors.map((c: ShoppingListContributor) => (
              <li key={c.requirement_id} className="rounded-md border border-border p-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{c.product_name}</span>
                  <span className="text-muted-foreground">needed {formatDate(c.demand_date)}</span>
                  {c.is_overdue && <Badge variant="destructive">Overdue</Badge>}
                  {c.is_in_production && <Badge variant="secondary">In production</Badge>}
                  <span className="ml-auto">
                    reserves {qty(c.reserved_quantity, item.canonical_unit)}
                  </span>
                </div>
                {c.orders.length > 0 && (
                  <ul className="mt-1 flex flex-col gap-0.5 pl-3 text-xs text-muted-foreground">
                    {c.orders.map((o) => (
                      <li key={o.order_id}>
                        <Link
                          to={`/app/orders/${o.order_id}`}
                          className="underline-offset-4 hover:underline"
                        >
                          {o.order_number}
                        </Link>{" "}
                        · {o.customer_name ?? "Guest"}
                        {o.fulfillment_date && ` · ${formatDate(o.fulfillment_date)}`}
                        {o.fulfillment_time && ` ${formatTime(o.fulfillment_time)}`} ·{" "}
                        {formatQuantityForDisplay(o.demand_quantity)} units of the product
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <h3 className="text-sm font-semibold">Cumulative shortage by date</h3>
        <ul className="mt-1 flex flex-col gap-0.5 text-sm">
          {changePoints.map((entry) => (
            <li key={entry.demand_date}>
              {formatDate(entry.demand_date)}:{" "}
              {qty(entry.cumulative_reserved_through_date, item.canonical_unit)} reserved
              {Number(entry.cumulative_shortage_through_date) > 0
                ? ` — short ${qty(entry.cumulative_shortage_through_date, item.canonical_unit)}`
                : " — covered"}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function ItemRow({ item, showShortage }: { item: ShoppingListItem; showShortage: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <TableRow>
        <TableCell className="font-medium">{item.ingredient_name}</TableCell>
        <TableCell>
          {showShortage ? (
            <span className="font-medium text-destructive">
              Short {qty(item.shortage_quantity, item.canonical_unit)}
            </span>
          ) : (
            <Badge variant="outline">Covered</Badge>
          )}
        </TableCell>
        <TableCell>{item.needed_by_date ? formatDate(item.needed_by_date) : "—"}</TableCell>
        <TableCell>{qty(item.physical_quantity, item.canonical_unit)}</TableCell>
        <TableCell>{qty(item.reserved_through_end, item.canonical_unit)}</TableCell>
        <TableCell>
          <Button
            variant="outline"
            size="sm"
            aria-expanded={open}
            aria-label={`${open ? "Hide" : "Show"} contributing demand for ${item.ingredient_name}`}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "Hide demand" : "Show demand"}
          </Button>
        </TableCell>
      </TableRow>
      {open && (
        <TableRow>
          <TableCell colSpan={6} className="bg-muted/30">
            <Contributors item={item} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function ItemsTable({ items, showShortage }: { items: ShoppingListItem[]; showShortage: boolean }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Ingredient</TableHead>
          <TableHead>{showShortage ? "Shortage to buy" : "Status"}</TableHead>
          <TableHead>Needed by</TableHead>
          <TableHead>Physical on hand</TableHead>
          <TableHead>Reserved through end</TableHead>
          <TableHead />
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <ItemRow key={item.ingredient_id} item={item} showShortage={showShortage} />
        ))}
      </TableBody>
    </Table>
  );
}

export function ShoppingListPage() {
  const [selection, setSelection] = useState<ShoppingListSelection>({ kind: "default" });
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [customError, setCustomError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["shopping-list", selection],
    queryFn: () => shoppingListApi.get(selection),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });

  const report = query.data;
  const activeHorizon = report && !report.range.is_custom ? report.range.horizon_days : null;
  const isCustom = selection.kind === "custom";

  const applyCustom = () => {
    if (!startDate || !endDate) {
      setCustomError("Choose both a start date and an end date.");
      return;
    }
    if (endDate < startDate) {
      setCustomError("The end date cannot be before the start date.");
      return;
    }
    setCustomError(null);
    setSelection({ kind: "custom", startDate, endDate });
  };

  const serverIssues =
    query.error instanceof ApiError && query.error.status === 422
      ? (query.error.body?.error.issues ?? [])
      : [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">Shopping List</h1>
        <Link to="/app/production" className={buttonVariants({ variant: "outline", size: "sm" })}>
          Production Plan
        </Link>
      </div>
      <p className="text-sm text-muted-foreground">
        What to buy to cover confirmed demand, derived from active demand and current inventory. It
        updates automatically as orders and stock change.
      </p>

      <div className="flex flex-wrap items-end gap-2" role="group" aria-label="Planning horizon">
        <Button
          variant={activeHorizon === 3 && !isCustom ? "default" : "outline"}
          size="sm"
          aria-pressed={activeHorizon === 3 && !isCustom}
          onClick={() => setSelection({ kind: "preset", horizonDays: 3 })}
        >
          3 days
        </Button>
        <Button
          variant={activeHorizon === 7 && !isCustom ? "default" : "outline"}
          size="sm"
          aria-pressed={activeHorizon === 7 && !isCustom}
          onClick={() => setSelection({ kind: "preset", horizonDays: 7 })}
        >
          7 days
        </Button>
        <Button
          variant={isCustom ? "default" : "outline"}
          size="sm"
          aria-pressed={isCustom}
          onClick={applyCustom}
        >
          Apply custom range
        </Button>
        <div className="flex flex-col gap-1">
          <label htmlFor="shopping-start" className="text-xs font-medium">
            Start date
          </label>
          <Input
            id="shopping-start"
            type="date"
            value={startDate}
            min={report?.business_today}
            onChange={(e) => setStartDate(e.target.value)}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="shopping-end" className="text-xs font-medium">
            End date
          </label>
          <Input
            id="shopping-end"
            type="date"
            value={endDate}
            min={startDate || report?.business_today}
            onChange={(e) => setEndDate(e.target.value)}
            className="w-40"
          />
        </div>
      </div>
      {customError && (
        <p role="alert" className="text-sm text-destructive">
          {customError}
        </p>
      )}
      {serverIssues.length > 0 && (
        <ul role="alert" className="text-sm text-destructive">
          {serverIssues.map((issue) => (
            <li key={issue.code}>{issue.message}</li>
          ))}
        </ul>
      )}

      {report && (
        <p className="text-sm" data-testid="range-summary">
          Showing demand through <strong>{formatDate(report.range.end_date)}</strong> (
          {report.range.is_custom
            ? `custom range from ${formatDate(report.range.start_date)}`
            : `${report.range.horizon_days}-day horizon from today`}
          ). Earlier and overdue demand is always included.
        </p>
      )}

      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : query.isError && serverIssues.length === 0 ? (
        <ErrorBanner error={query.error} onRetry={() => query.refetch()} />
      ) : report ? (
        <>
          {report.incomplete_recipe.length > 0 && (
            <div
              role="status"
              className="rounded-md border border-border bg-muted/40 p-3 text-sm"
              data-testid="incomplete-recipe-notice"
            >
              <p className="font-medium">Some demand can't be planned yet</p>
              <p className="text-muted-foreground">
                These products have confirmed demand but no applicable recipe, so their ingredient
                needs are not included below:
              </p>
              <ul className="mt-1 list-disc pl-5">
                {report.incomplete_recipe.map((n) => (
                  <li key={n.requirement_id}>
                    <Link
                      to={`/app/products/${n.product_id}`}
                      className="underline-offset-4 hover:underline"
                    >
                      {n.product_name}
                    </Link>{" "}
                    — {formatQuantityForDisplay(n.demand_quantity)} units on{" "}
                    {formatDate(n.demand_date)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {report.shortages.length === 0 ? (
            <p className="text-sm" data-testid="no-shortages">
              {report.covered.length === 0
                ? "No ingredient demand in this range."
                : "Nothing to buy — current stock covers all ingredient demand in this range."}
            </p>
          ) : (
            <section aria-labelledby="shortage-heading" className="flex flex-col gap-2">
              <h2 id="shortage-heading" className="text-sm font-semibold">
                Need to buy ({report.shortages.length})
              </h2>
              <ItemsTable items={report.shortages} showShortage />
            </section>
          )}

          {report.covered.length > 0 && (
            <details className="rounded-md border border-border p-2">
              <summary className="cursor-pointer text-sm font-medium">
                Covered by current stock ({report.covered.length})
              </summary>
              <div className="mt-2">
                <ItemsTable items={report.covered} showShortage={false} />
              </div>
            </details>
          )}
        </>
      ) : null}
    </div>
  );
}
