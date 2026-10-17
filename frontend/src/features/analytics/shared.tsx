// Small presentational pieces shared by the Analytics tabs. Display only — every figure is a
// server-provided string (Spec §12.5); nothing is recomputed here.

import type { ReactNode } from "react";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { buttonVariants } from "../../components/ui/button";
import { cn } from "cn";
import type { ReportHeader } from "./api";
import { describePeriod } from "./formatting";

export function PeriodSummary({ header }: { header: ReportHeader }) {
  return (
    <p className="text-sm text-muted-foreground" data-testid="period-summary">
      {describePeriod(header.period)} · Business timezone {header.timezone} · Orders counted by the
      day they were completed
    </p>
  );
}

export function SectionHeading({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <h2 id={id} className="text-base font-semibold">
      {children}
    </h2>
  );
}

export function CsvLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      download
      className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
      data-testid="csv-link"
    >
      {label}
    </a>
  );
}

interface KpiCardProps {
  label: string;
  value: string;
  caption?: string;
  internal?: boolean;
  delta?: ReactNode;
  testId: string;
}

export function KpiCard({ label, value, caption, internal, delta, testId }: KpiCardProps) {
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-lg border p-4",
        internal ? "border-dashed border-border bg-muted/30" : "border-border",
      )}
      data-testid={testId}
    >
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      {caption && <span className="text-xs text-muted-foreground">{caption}</span>}
      {delta && <span className="text-xs">{delta}</span>}
    </div>
  );
}

interface SelectFieldProps<T extends string> {
  id: string;
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}

export function SelectField<T extends string>({
  id,
  label,
  value,
  options,
  onChange,
}: SelectFieldProps<T>) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value as T)}
        className="h-8 rounded-lg border border-input bg-background px-2 text-sm"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
      {children}
    </p>
  );
}

export function Pager({
  total,
  limit,
  offset,
  onOffset,
  noun,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffset: (offset: number) => void;
  noun: string;
}) {
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + limit, total);
  return (
    <nav className="flex items-center justify-between gap-2 text-sm" aria-label={`${noun} pages`}>
      <span data-testid="pager-range">
        {first}–{last} of {total} {noun}
      </span>
      <span className="flex gap-2">
        <button
          type="button"
          className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
          disabled={offset === 0}
          onClick={() => onOffset(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <button
          type="button"
          className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
          disabled={offset + limit >= total}
          onClick={() => onOffset(offset + limit)}
        >
          Next
        </button>
      </span>
    </nav>
  );
}

/**
 * Query-failure display for every Analytics tab. A structured `ANALYTICS_RANGE_INVALID` 422
 * (including `ANALYTICS_RANGE_TOO_LARGE`) is a problem with the selected period, not a transient
 * failure: show the server's own messages and no Retry button. Anything else is the shared
 * retryable banner.
 */
export function AnalyticsError({
  error,
  onRetry,
  fallbackMessage,
}: {
  error: unknown;
  onRetry: () => void;
  fallbackMessage: string;
}) {
  if (
    error instanceof ApiError &&
    error.status === 422 &&
    error.body?.error.code === "ANALYTICS_RANGE_INVALID"
  ) {
    const issues = error.body.error.issues;
    return (
      <div
        role="alert"
        className="flex flex-col gap-1 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
      >
        <span className="font-medium">{error.body.error.message}</span>
        {issues.length > 0 && (
          <ul className="list-disc pl-5">
            {issues.map((issue) => (
              <li key={`${issue.code}-${issue.field ?? ""}`}>{issue.message}</li>
            ))}
          </ul>
        )}
        <span className="text-xs">Choose a different period above to see this report.</span>
      </div>
    );
  }
  return <ErrorBanner error={error} onRetry={onRetry} fallbackMessage={fallbackMessage} />;
}
