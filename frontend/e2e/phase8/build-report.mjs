// Builds phase8_acceptance_results.json and PHASE8_AUTOMATED_ACCEPTANCE_REPORT.md from the
// per-test evidence records written by helpers.ts (TEST-ONLY tooling).
// Usage: node e2e/phase8/build-report.mjs

import fs from "node:fs";
import path from "node:path";

const OUT = path.resolve(import.meta.dirname, "../../../test-artifacts/phase8");
const results = fs
  .readdirSync(path.join(OUT, "results"))
  .filter((f) => f.endsWith(".json"))
  .map((f) => JSON.parse(fs.readFileSync(path.join(OUT, "results", f), "utf8")));

const META = [
  ["T01", "1", "Normal Start / no Ingredient consumption", "Fresh tenant; 2 ingredients (flour 10000 g @0.01, sugar 5000 g @0.02); Produced product yield 6/batch (120 g + 60 g); Order qty 9.", "Create+confirm Order (UI) -> Production Plan -> Review & Start -> Start recommended -> open run."],
  ["T02", "2", "Underproduction warning + stale acknowledgment", "Same recipe; confirmed Order qty 13 (3 recommended batches).", "Override batches to 2 -> warning; second page edits Order 13->19; original page clicks the stale 'Start anyway'; re-enter, fresh warning, accept."],
  ["T03A", "3A", "Cancel run — basic", "Confirmed Order qty 6; run started.", "Cancel Run via UI dialog."],
  ["T03B", "3B", "Cancel releases protected Surplus / readiness", "Surplus lot 5 (UI); Order A qty 10 (later); Order B qty 5 (earlier).", "Confirm A, start A's run, confirm B, cancel A's run."],
  ["T04", "4", "Finish with exact required output", "Confirmed Order qty 9; run started.", "Finish with output 9, overrides blank."],
  ["T05", "5", "Actual underproduction / shortfall", "Confirmed Order qty 10.", "Finish first run with 4; start continuation run; finish with 6."],
  ["T06", "6", "Overproduction creates real Surplus", "Product default usable days 7, reusable; Order qty 5.", "Start, finish with output 8; inspect Surplus list/history."],
  ["T07", "7", "Zero-output failed batch", "Confirmed Order qty 9.", "Finish with output 0."],
  ["T08", "8", "Actual ingredient usage overrides", "Confirmed Order qty 6.", "Finish with flour 130 g, sugar 55 g, output 6."],
  ["T09", "9", "Ingredient inventory driven negative", "Flour stock 100 g (< needed); Order qty 6.", "Acknowledge shortage on confirm; finish with flour override 250 g."],
  ["T10", "10", "Existing Surplus -> READY immediately", "Surplus lot 5 (UI); Order qty 5.", "Confirm Order."],
  ["T11A", "11A", "Consequential negative Surplus adjustment", "Surplus 5; Order qty 5 READY.", "Remove 1 -> warning; 'Adjust anyway'."],
  ["T11B", "11B", "Active-run floor hard block", "Surplus 3; Order qty 6; run started.", "Attempt to remove 1 (rejected); then cancel run."],
  ["T12", "12", "Purchased-only Order READY, no consumption", "Purchased product stock 10; Order qty 2.", "Confirm Order."],
  ["T13", "13", "Custom Item manual satisfaction", "Custom-Item-only Order; plus a Purchased-line Order for the negative case.", "Confirm; Mark satisfied; Mark unsatisfied; attempt on non-Custom line."],
  ["T14", "14", "Mixed Order requires all categories", "Produced 6 + Purchased 2 + Custom Item; purchased stock 10.", "Confirm; mark Custom satisfied; start+finish production."],
  ["T15", "15", "Usable-through / Business-local date", "Lots A (through D-1) and B (through D), 5 each; browser tz Pacific/Kiritimati, Business tz America/New_York.", "Confirm Order qty 5 for date D."],
  ["T16", "16", "READY + active-run overlap (mandatory regression)", "Surplus 3; Orders B (6, earliest), A (3), C (2); recipe as above.", "Steps A-D of the scenario, incl. new lot + hard-floor rejection."],
];

const rows = META.map(([prefix, num, scenario, setup, actions]) => {
  const r = results.find((x) => x.test.startsWith(prefix + " "));
  return {
    test: num,
    scenario,
    result: r ? (r.status === "passed" ? "PASS" : "FAIL") : "NOT RUN",
    setup,
    actions,
    keyAssertions: r?.notes ?? [],
    screenshots: r?.screenshots ?? [],
    entityIds: r?.entityIds ?? {},
    unexpectedResponses: r?.unexpectedResponses ?? [],
    consoleErrors: r?.consoleErrors ?? [],
    fatalErrors: r?.fatalErrors ?? [],
    cleanup: r?.cleanup ?? "n/a",
    trace: r?.trace,
    failureScreenshot: r?.failureScreenshot,
    finalUrl: r?.finalUrl,
    durationMs: r?.durationMs,
  };
});

const runs = [1, 2, 3, 4]
  .map((i) => path.join(OUT, `run${i}-report.json`))
  .filter((f) => fs.existsSync(f))
  .map((f, idx) => {
    const j = JSON.parse(fs.readFileSync(f, "utf8"));
    return { run: idx + 1, ...j.stats };
  });

const matrix = {
  generatedAt: new Date().toISOString(),
  total: rows.length,
  passed: rows.filter((r) => r.result === "PASS").length,
  failed: rows.filter((r) => r.result === "FAIL").length,
  suiteRuns: runs,
  tests: rows,
};
fs.writeFileSync(path.join(OUT, "phase8_acceptance_results.json"), JSON.stringify(matrix, null, 2));

const md = [];
md.push("# Phase 8 Automated Acceptance — Result Matrix\n");
md.push(`Generated ${matrix.generatedAt}. Playwright tests: ${matrix.total}; passed ${matrix.passed}; failed ${matrix.failed}.\n`);
md.push("| Test | Scenario | Result | Evidence |\n| --- | --- | --- | --- |");
for (const r of rows) {
  const ev = [r.screenshots.length ? `${r.screenshots.length} screenshot(s)` : "", r.trace ? `trace ${r.trace}` : "", `cleanup ${r.cleanup}`]
    .filter(Boolean)
    .join("; ");
  md.push(`| ${r.test} | ${r.scenario} | ${r.result} | ${ev} |`);
}
md.push("");
for (const r of rows) {
  md.push(`## Test ${r.test} — ${r.scenario}: ${r.result}\n`);
  md.push(`- **Fixture/setup:** ${r.setup}`);
  md.push(`- **Actions:** ${r.actions}`);
  md.push("- **Key assertions / final state:**");
  for (const n of r.keyAssertions) md.push(`  - ${n}`);
  md.push(`- **Screenshots:** ${r.screenshots.join(", ") || "none"}`);
  md.push(`- **Console/network:** console errors ${r.consoleErrors.length}; page errors ${r.fatalErrors.length}; unexpected HTTP errors ${r.unexpectedResponses.length}`);
  if (r.result === "FAIL") md.push(`- **Failure evidence:** ${r.failureScreenshot ?? ""} ${r.trace ?? ""}`);
  md.push("");
}
fs.writeFileSync(path.join(OUT, "PHASE8_ACCEPTANCE_REPORT.md"), md.join("\n"));
console.log(JSON.stringify({ total: matrix.total, passed: matrix.passed, failed: matrix.failed, runs }));
