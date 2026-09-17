// Human-readable display formatting for the API's fixed-precision NUMERIC(18,6) Decimal
// strings (e.g. "12.000000", "0.000001") — display-only, never used to build a request
// payload. Pure string manipulation: the value is never routed through JS `Number`, so no
// precision is ever at risk of being lost for a value this column shape can hold.

export function formatDecimal(value: string): string {
  if (!value.includes(".")) {
    return value;
  }
  return value.replace(/0+$/, "").replace(/\.$/, "");
}

// Additive wrapper for the Manual Acceptance UX Correction Plan (Finding 3): trims
// trailing-zero storage precision via `formatDecimal`, then adds a thousands
// separator to the integer portion only. Never used to build a request payload,
// and never applied at `formatDecimal`'s own existing call sites — display-only,
// on top of display-only.
export function formatQuantityForDisplay(value: string): string {
  const trimmed = formatDecimal(value);
  const negative = trimmed.startsWith("-");
  const unsigned = negative ? trimmed.slice(1) : trimmed;
  const [integerPart, fractionalPart] = unsigned.split(".");
  const groupedIntegerPart = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const result =
    fractionalPart !== undefined ? `${groupedIntegerPart}.${fractionalPart}` : groupedIntegerPart;
  return negative ? `-${result}` : result;
}
