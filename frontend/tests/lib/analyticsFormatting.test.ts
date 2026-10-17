import { describe, expect, it } from "vitest";
import {
  describePeriod,
  formatDateWithYear,
  formatPercentagePoints,
  formatRatioAsPercent,
  formatSignedCount,
  formatSignedMoney,
  formatSignedPercent,
} from "../../src/features/analytics/formatting";

describe("formatRatioAsPercent", () => {
  it("shifts a 6-dp ratio to a percent by string arithmetic", () => {
    expect(formatRatioAsPercent("0.716566")).toBe("71.7%");
    expect(formatRatioAsPercent("0.716566", 2)).toBe("71.66%");
    expect(formatRatioAsPercent("1.000000")).toBe("100.0%");
    expect(formatRatioAsPercent("0.900000", 0)).toBe("90%");
    expect(formatRatioAsPercent("3.256410")).toBe("325.6%");
  });

  it("rounds half away from zero and handles negatives", () => {
    expect(formatRatioAsPercent("0.000500")).toBe("0.1%");
    expect(formatRatioAsPercent("0.000499")).toBe("0.0%");
    expect(formatRatioAsPercent("-0.500000")).toBe("-50.0%");
    expect(formatRatioAsPercent("-0.001383")).toBe("-0.1%");
  });

  it("never prints a negative zero and renders null as an em dash", () => {
    expect(formatRatioAsPercent("-0.000001")).toBe("0.0%");
    expect(formatRatioAsPercent("0.000000")).toBe("0.0%");
    expect(formatRatioAsPercent(null)).toBe("—");
  });
});

describe("signed formatters", () => {
  it("signs money, percent, points and counts", () => {
    expect(formatSignedMoney("127.00")).toBe("+$127.00");
    expect(formatSignedMoney("-36.05")).toBe("-$36.05");
    expect(formatSignedMoney("0.00")).toBe("$0.00");
    expect(formatSignedPercent("3.256410")).toBe("+325.6%");
    expect(formatSignedPercent("-0.250000")).toBe("-25.0%");
    expect(formatPercentagePoints("-0.001383")).toBe("-0.1 pts");
    expect(formatPercentagePoints("0.050000")).toBe("+5.0 pts");
    expect(formatSignedCount(5)).toBe("+5");
    expect(formatSignedCount(-2)).toBe("-2");
    expect(formatSignedCount(0)).toBe("0");
  });
});

describe("period text", () => {
  it("formats Business-local dates without the browser timezone", () => {
    expect(formatDateWithYear("2026-10-16")).toBe("Oct 16, 2026");
    const period = {
      range: "LAST_7_DAYS" as const,
      start_date: "2026-10-10",
      end_date: "2026-10-16",
      days: 7,
      lower_bound_utc: "",
      upper_bound_utc: "",
    };
    expect(describePeriod(period)).toBe("Oct 10, 2026 – Oct 16, 2026 (7 days)");
    expect(describePeriod({ ...period, end_date: "2026-10-10", days: 1 })).toBe(
      "Oct 10, 2026 (1 day)",
    );
  });
});
