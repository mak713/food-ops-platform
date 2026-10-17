// Display-only formatting for Analytics (Spec §12.19). Nothing here computes a business
// value: the backend is authoritative (Spec §12.5). Ratios arrive as 6-dp Decimal strings
// (0.716566 = 71.6566%), so they are shifted and rounded by string/BigInt arithmetic — never
// routed through JS floating point.

import { formatMoney } from "../../lib/format";
import type { Period } from "./api";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `2026-10-16` -> `Oct 16, 2026` (Business-local dates are never routed through the browser tz). */
export function formatDateWithYear(isoDate: string): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  return `${MONTHS[month - 1]} ${day}, ${year}`;
}

/**
 * `0.716566` -> `71.7%`, `-0.001383` -> `-0.1%` (round half away from zero, `digits` dp).
 * `null` is "not defined" (e.g. margin with zero Revenue) and renders as an em dash.
 */
export function formatRatioAsPercent(value: string | null, digits = 1): string {
  if (value === null) return "—";
  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [intPart, fracRaw = ""] = unsigned.split(".");
  const frac = fracRaw.padEnd(6, "0").slice(0, 6);
  // ratio * 100 = N / 10^4 percent, where N is the ratio in micro-units.
  const micro = BigInt(intPart + frac);
  const divisor = 10n ** BigInt(4 - digits);
  const scaled = (micro + divisor / 2n) / divisor; // percent in 10^-digits units
  const text = scaled.toString().padStart(digits + 1, "0");
  const whole = text.slice(0, text.length - digits);
  const fraction = text.slice(text.length - digits);
  const isZero = scaled === 0n;
  return `${negative && !isZero ? "-" : ""}${whole}${digits > 0 ? `.${fraction}` : ""}%`;
}

/** `127.00` -> `+$127.00`, `-36.05` -> `-$36.05`, `0.00` -> `$0.00`. */
export function formatSignedMoney(value: string): string {
  const formatted = formatMoney(value);
  if (value.startsWith("-")) return formatted;
  return Number(value) === 0 ? formatted : `+${formatted}`;
}

/** Signed percent for a growth ratio: `3.256410` -> `+325.6%`. */
export function formatSignedPercent(value: string): string {
  const text = formatRatioAsPercent(value, 1);
  return text.startsWith("-") || text === "0.0%" ? text : `+${text}`;
}

/** Percentage points for a margin (rate) delta: `-0.001383` -> `-0.1 pts`. */
export function formatPercentagePoints(value: string): string {
  const text = formatRatioAsPercent(value, 1).replace("%", "");
  return `${text.startsWith("-") || text === "0.0" ? text : `+${text}`} pts`;
}

export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

export function formatSignedCount(value: number): string {
  return value > 0 ? `+${formatCount(value)}` : formatCount(value);
}

export const INTERNAL_NOTE =
  "Internal business metric — not a customer-facing amount. Direct Cost is operational " +
  "historical direct cost, not accounting profit.";

export function describePeriod(period: Period): string {
  const range =
    period.start_date === period.end_date
      ? formatDateWithYear(period.start_date)
      : `${formatDateWithYear(period.start_date)} – ${formatDateWithYear(period.end_date)}`;
  return `${range} (${period.days} ${period.days === 1 ? "day" : "days"})`;
}
