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
