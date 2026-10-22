import { describe, expect, it } from "vitest";
import {
  MARGIN_PERCENT_ERROR,
  percentToRatio,
  ratioToPercent,
} from "../../src/features/settings/ratio";

describe("percentToRatio (decimal-string shift, no float math)", () => {
  it.each([
    ["60", "0.600000"],
    ["62.5", "0.625000"],
    ["0", "0.000000"],
    ["0.0001", "0.000001"],
    ["5.5", "0.055000"],
    ["99.9999", "0.999999"],
    ["007", "0.070000"],
    ["  12.25  ", "0.122500"],
    ["33.3333", "0.333333"],
  ])("%s%% -> %s", (input, expected) => {
    expect(percentToRatio(input)).toEqual({ ok: true, ratio: expected });
  });

  it("treats blank input as no target", () => {
    expect(percentToRatio("")).toEqual({ ok: true, ratio: null });
    expect(percentToRatio("   ")).toEqual({ ok: true, ratio: null });
  });

  it.each([
    "100",
    "100.0",
    "150",
    "-1",
    "-0.5",
    "abc",
    "1e2",
    "12.",
    ".5",
    "1,5",
    "62.50001",
    "NaN",
    "%",
    "5%",
  ])("rejects %s", (input) => {
    expect(percentToRatio(input)).toEqual({ ok: false, message: MARGIN_PERCENT_ERROR });
  });

  it("is exact where binary floating point is not (0.07 * 100, 4.35 * 100, 1.15 * 100)", () => {
    // These are the classic float-error inputs; a Number-based conversion gets them wrong.
    expect(percentToRatio("7")).toEqual({ ok: true, ratio: "0.070000" });
    expect(percentToRatio("4.35")).toEqual({ ok: true, ratio: "0.043500" });
    expect(percentToRatio("1.15")).toEqual({ ok: true, ratio: "0.011500" });
    expect(0.07 * 100).not.toBe(7); // documents why a float path would be wrong
  });
});

describe("ratioToPercent", () => {
  it.each([
    ["0.625000", "62.5"],
    ["0.600000", "60"],
    ["0.000000", "0"],
    ["0.000001", "0.0001"],
    ["0.055000", "5.5"],
    ["0.999999", "99.9999"],
    ["0.5", "50"],
  ])("%s -> %s%%", (ratio, expected) => {
    expect(ratioToPercent(ratio)).toBe(expected);
  });

  it("shows nothing for no target", () => {
    expect(ratioToPercent(null)).toBe("");
  });

  it("round-trips every 4-decimal percentage boundary", () => {
    for (const percent of ["0", "0.0001", "0.5", "10", "62.5", "99.9999"]) {
      const parsed = percentToRatio(percent);
      expect(parsed.ok).toBe(true);
      if (parsed.ok) expect(ratioToPercent(parsed.ratio)).toBe(percent);
    }
  });
});
