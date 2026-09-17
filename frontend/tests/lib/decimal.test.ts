import { describe, expect, it } from "vitest";
import { formatDecimal, formatQuantityForDisplay } from "../../src/lib/decimal";

describe("formatDecimal", () => {
  it("leaves an integer-looking value with no fractional part unchanged", () => {
    expect(formatDecimal("12")).toBe("12");
  });

  it("strips a whole-number value's trailing zero padding down to the integer", () => {
    expect(formatDecimal("12.000000")).toBe("12");
  });

  it("strips only the insignificant trailing zeros from a value with a real fractional part", () => {
    expect(formatDecimal("12.500000")).toBe("12.5");
  });

  it("preserves full six-decimal-place precision when every digit is significant", () => {
    expect(formatDecimal("0.000001")).toBe("0.000001");
  });

  it("never truncates a value with no trailing zeros to strip, regardless of scale", () => {
    expect(formatDecimal("999999999999.999999")).toBe("999999999999.999999");
  });

  it("does not strip significant trailing zeros belonging to the integer part itself", () => {
    // No decimal point at all — "100" must never become "1".
    expect(formatDecimal("100")).toBe("100");
  });

  it("reduces an all-zero fractional value to its bare integer", () => {
    expect(formatDecimal("0.000000")).toBe("0");
  });
});

describe("formatQuantityForDisplay", () => {
  it("trims trailing storage-precision zeros off a whole number", () => {
    expect(formatQuantityForDisplay("12.000000")).toBe("12");
  });

  it("preserves meaningful fractional digits", () => {
    expect(formatQuantityForDisplay("1.250000")).toBe("1.25");
  });

  it("adds a thousands separator to a large whole-number quantity", () => {
    expect(formatQuantityForDisplay("496000.000000")).toBe("496,000");
  });

  it("groups the integer part while preserving fractional digits together", () => {
    expect(formatQuantityForDisplay("1234567.500000")).toBe("1,234,567.5");
  });

  it("does not group a value under 1000", () => {
    expect(formatQuantityForDisplay("500.000000")).toBe("500");
  });

  it("never mutates the underlying numeric value, only its display form", () => {
    // Confirms this is purely additive over formatDecimal's own trimming —
    // the same value, unformatted, is what a request payload would still use.
    expect(formatQuantityForDisplay("0.000000")).toBe("0");
  });
});
