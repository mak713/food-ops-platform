// Contribution-margin representation (Phase 11 Revision 2): the database/API value is a ratio
// (NUMERIC(9,6), e.g. "0.625000"); the UI shows and accepts a percentage ("62.5"). The
// conversion is a pure decimal-string shift by two places — it never routes through JS
// `Number`, so no floating-point error can reach an authoritative value (62.5% -> 0.625000
// exactly; a naive 62.5/100 style conversion is not guaranteed to be).
//
// Percentages accept up to 4 decimal places (so the ratio never exceeds 6) and must be at
// least 0 and strictly below 100 (the backend CHECK is 0 <= ratio < 1).

export const MARGIN_PERCENT_ERROR =
  "Enter a percentage from 0 up to (but not including) 100, with at most 4 decimal places.";

const PERCENT_PATTERN = /^(\d{1,3})(?:\.(\d{1,4}))?$/;
const RATIO_PATTERN = /^(\d+)(?:\.(\d{1,6}))?$/;

export type PercentParse = { ok: true; ratio: string | null } | { ok: false; message: string };

/** "62.5" -> "0.625000"; blank -> null (no target); anything out of range/format -> error. */
export function percentToRatio(input: string): PercentParse {
  const trimmed = input.trim();
  if (trimmed === "") return { ok: true, ratio: null };
  const match = PERCENT_PATTERN.exec(trimmed);
  if (!match) return { ok: false, message: MARGIN_PERCENT_ERROR };
  const [, whole, fraction = ""] = match;
  // percent * 10^4 as an integer string, then ratio = that * 10^-6.
  const scaled = (whole + fraction.padEnd(4, "0")).replace(/^0+(?=\d)/, "");
  if (scaled.length > 6) return { ok: false, message: MARGIN_PERCENT_ERROR }; // >= 100%
  return { ok: true, ratio: `0.${scaled.padStart(6, "0")}` };
}

/** "0.625000" -> "62.5"; null -> "" (display only; never sent to the server). */
export function ratioToPercent(ratio: string | null): string {
  if (ratio === null) return "";
  const match = RATIO_PATTERN.exec(ratio);
  if (!match || match[1] !== "0") return ratio;
  const digits = (match[2] ?? "").padEnd(6, "0");
  const whole = digits.slice(0, 2).replace(/^0+(?=\d)/, "");
  const fraction = digits.slice(2).replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole;
}
