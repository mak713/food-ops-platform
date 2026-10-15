// Phase 8 Automated Manual-Acceptance Pass — Tests 2–9 (production lifecycle).
// Browser/UI -> React -> FastAPI -> services -> PostgreSQL. Each test owns a fresh tenant.

import type { Page } from "@playwright/test";
import { addDays, expect, type Json, test, type Tenant } from "./helpers";

async function expectOrderUI(t: Tenant, orderId: string, status: string, page: Page = t.page) {
  await page.goto(`/app/orders/${orderId}`);
  await expect(page.getByText(status, { exact: true }).first()).toBeVisible();
  expect((await t.order(orderId)).status).toBe(status);
}

const lineOf = async (t: Tenant, orderId: string, index = 0) =>
  ((await t.order(orderId)).lines as Json[])[index].id as string;

const surplusFor = (t: Tenant, lineId: string) =>
  Number(t.q(`select coalesce(sum(quantity),0) from surplus_allocations where order_line_id='${lineId}'`)[0][0]);
const completedFor = (t: Tenant, lineId: string) =>
  Number(
    t.q(`select coalesce(sum(quantity),0) from production_run_order_allocations where order_line_id='${lineId}'`)[0][0],
  );

async function planRow(t: Tenant, productName: string, demandDate?: string) {
  await t.page.goto("/app/production");
  let row = t.page.getByRole("row").filter({ hasText: productName });
  if (demandDate) row = row.filter({ hasText: demandDate });
  return row.first();
}

test("T02 Underproduction warning and stale acknowledgment", async ({ t }) => {
  t.expectResponses("UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED", "PRODUCTION_REQUIREMENT_CHANGED", "NOT_FOUND");
  const { product } = await t.standardProduced();
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 13 }]);
  await t.confirmOrderUI(orderId);
  const page = t.page;

  await t.openReviewAndStart(product.name);
  await page.locator("#batches").fill("2");
  const first = page.waitForResponse(
    (r) => r.url().includes("/api/v1/production-runs") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Start Production" }).click();
  const firstBody = (await (await first).json()) as Json;
  const fp1 = firstBody.error.issues[0].details.fingerprint as string;
  await expect(page.getByText("This batch count will not fully cover demand")).toBeVisible();
  await t.shot(page, "underproduction-warning");
  expect(((await t.api.get("/production-runs")).items as Json[]).length).toBe(0);
  t.note(`Underproduction warning shown, run NOT started; fingerprint #1 issued (${fp1.slice(0, 12)}...)`);

  // A second page edits the confirmed Order so demand materially changes (13 -> 19).
  const page2 = await t.newPage();
  await page2.goto(`/app/orders/${orderId}/edit`);
  await page2.locator('[id="lines.0.underlying_quantity"]').fill("19");
  await page2.getByRole("button", { name: "Save", exact: true }).click();
  await page2.waitForURL(new RegExp(`/app/orders/${orderId}$`));
  t.note("Second page edited confirmed Order quantity 13 -> 19");

  // Original page reuses the now-stale acknowledgment.
  const stale = page.waitForResponse(
    (r) => r.url().includes("/api/v1/production-runs") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Start anyway" }).click();
  const staleResp = await stale;
  const staleBody = (await staleResp.json()) as Json;
  t.note(`Stale acknowledgment -> HTTP ${staleResp.status()} ${staleBody?.error?.code}`);
  await t.shot(page, "stale-ack-rejected");
  expect(staleResp.ok(), "stale acknowledgment must not start a run").toBe(false);
  expect(((await t.api.get("/production-runs")).items as Json[]).length).toBe(0);

  // Obtain the freshly-current warning and accept it.
  let fp2: string;
  if (staleBody.error.code === "UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED") {
    fp2 = staleBody.error.issues[0].details.fingerprint;
    t.note("Server answered the stale acknowledgment with a fresh warning/fingerprint directly");
  } else {
    t.note("Demand change replaced the requirement; re-entering through the Production Plan for the current one");
    await t.openReviewAndStart(product.name);
    await page.locator("#batches").fill("3");
    const fresh = page.waitForResponse(
      (r) => r.url().includes("/api/v1/production-runs") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Start Production" }).click();
    fp2 = ((await (await fresh).json()) as Json).error.issues[0].details.fingerprint;
    await expect(page.getByText("This batch count will not fully cover demand")).toBeVisible();
  }
  expect(fp2).not.toBe(fp1);
  await t.shot(page, "fresh-warning");
  await page.getByRole("button", { name: "Start anyway" }).click();
  await page.waitForURL(/\/app\/production\/runs\/[0-9a-f-]{36}$/);
  const runId = page.url().split("/").pop() as string;
  t.entityIds.run = runId;
  const run = await t.run(runId);
  expect(run.status).toBe("IN_PRODUCTION");
  t.note(`Current acknowledgment accepted; run started with ${run.planned_batches} batches (fresh fingerprint differs)`);
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T03A Cancel run — no consumption, demand remains", async ({ t }) => {
  const { product, a, b } = await t.standardProduced();
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }]);
  await t.confirmOrderUI(orderId);
  const runId = await t.startRunUI(product.name);
  const before = { a: await t.ingredientQty(a.id), b: await t.ingredientQty(b.id) };
  await t.cancelRunUI(runId);
  await expect(t.page.getByRole("row").filter({ hasText: product.name })).toHaveCount(0);
  await t.page.goto(`/app/production/runs/${runId}`);
  await expect(t.page.getByText("Canceled", { exact: true })).toBeVisible();
  expect((await t.run(runId)).status).toBe("CANCELED");
  expect(await t.ingredientQty(a.id)).toBe(before.a);
  expect(await t.ingredientQty(b.id)).toBe(before.b);
  for (const id of [a.id, b.id])
    expect((await t.ingredientTxns(id)).filter((x) => x.transaction_type === "PRODUCTION_CONSUMPTION")).toEqual([]);
  const row = await planRow(t, product.name);
  await expect(row.getByRole("cell").nth(2)).toHaveText("6");
  await t.shot(t.page, "demand-returned-to-plan");
  t.note("Run CANCELED; ingredient balances unchanged; no consumption events; demand 6 back in the Production Plan");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T03B Cancel releases protected Surplus and re-prioritizes readiness", async ({ t }) => {
  const { product, a, b } = await t.standardProduced();
  await t.recordSurplusUI(product.name, "5");
  const orderA = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 10 }], { offsetDays: 5 });
  await t.confirmOrderUI(orderA);
  const lineA = await lineOf(t, orderA);
  expect(surplusFor(t, lineA)).toBe(5);
  await expect((await planRow(t, product.name)).getByRole("cell").nth(2)).toHaveText("5");
  t.note("Order A (10, later deadline): 5 Surplus-backed, production demand 5");
  const runA = await t.startRunUI(product.name);

  const orderB = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 5 }], { offsetDays: 2 });
  await t.confirmOrderUI(orderB);
  const lineB = await lineOf(t, orderB);
  expect(surplusFor(t, lineB), "B must not steal A's active-run-protected Surplus").toBe(0);
  expect(surplusFor(t, lineA)).toBe(5);
  await expectOrderUI(t, orderB, "CONFIRMED");
  await t.shot(t.page, "B-blocked-while-A-run-active");
  t.note("While A's run is active: B (earlier deadline) got 0 Surplus, A keeps 5");

  const before = { a: await t.ingredientQty(a.id), b: await t.ingredientQty(b.id) };
  await t.cancelRunUI(runA);
  expect((await t.run(runA)).status).toBe("CANCELED");
  expect(surplusFor(t, lineB)).toBe(5);
  expect(surplusFor(t, lineA)).toBe(0);
  await expectOrderUI(t, orderB, "READY");
  await t.shot(t.page, "B-ready-after-cancel");
  expect(await t.ingredientQty(a.id)).toBe(before.a);
  expect(await t.ingredientQty(b.id)).toBe(before.b);
  t.note("After Cancel (same transaction): B READY with the 5 Surplus, A released to 0; no ingredient consumption");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T04 Finish with exact required output", async ({ t }) => {
  const { product, a, b } = await t.standardProduced();
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 9 }]);
  await t.confirmOrderUI(orderId);
  const runId = await t.startRunUI(product.name);
  const before = { a: Number(await t.ingredientQty(a.id)), b: Number(await t.ingredientQty(b.id)) };
  await t.finishRunUI(runId, "9");
  await expect(t.page.getByText("Completed", { exact: true })).toBeVisible();
  await expect(t.page.getByText("Finished cleanly")).toBeVisible();
  await t.shot(t.page, "run-completed");
  const run = await t.run(runId);
  expect(run.status).toBe("COMPLETED");
  expect(Number(run.actual_output_quantity)).toBe(9);
  expect(run.actual_total_production_cost).not.toBeNull();
  for (const ri of run.run_ingredients as Json[]) expect(ri.actual_quantity).not.toBeNull();
  // 2 batches planned: 240 g flour + 120 g sugar consumed by default (Actual left blank).
  expect(Number(await t.ingredientQty(a.id))).toBe(before.a - 240);
  expect(Number(await t.ingredientQty(b.id))).toBe(before.b - 120);
  for (const id of [a.id, b.id])
    expect((await t.ingredientTxns(id)).some((x) => x.transaction_type === "PRODUCTION_CONSUMPTION")).toBe(true);
  await t.page.goto(`/app/inventory/ingredients/${a.id}/history`);
  await expect(t.page.getByText("-240").first()).toBeVisible();
  await t.shot(t.page, "ingredient-history");
  await expectOrderUI(t, orderId, "READY");
  await t.page.goto("/app/production");
  await expect(t.page.getByText("Nothing needs production right now.")).toBeVisible();
  t.note(
    `COMPLETED, output 9, actual cost ${run.actual_total_production_cost}; flour -240 g, sugar -120 g; order READY; no demand left`,
  );
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T05 Actual underproduction leaves shortfall; continuation covers only the remainder", async ({ t }) => {
  const { product } = await t.standardProduced();
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 10 }]);
  await t.confirmOrderUI(orderId);
  const line = await lineOf(t, orderId);
  const run1 = await t.startRunUI(product.name);
  await t.finishRunUI(run1, "4");
  await expect(t.page.getByText("did not fully cover its outstanding demand")).toBeVisible();
  await t.shot(t.page, "shortfall");
  expect((await t.run(run1)).status).toBe("COMPLETED");
  expect(completedFor(t, line)).toBe(4);
  await expectOrderUI(t, orderId, "CONFIRMED");
  await expect((await planRow(t, product.name)).getByRole("cell").nth(2)).toHaveText("6");
  expect(Number(t.q(`select count(*) from orders where business_id=$BIZ`)[0][0])).toBe(1);
  t.note("Run 1 COMPLETED with 4; 4 units allocated historically; order CONFIRMED; plan shows 6 remaining; still 1 Order");

  const run2 = await t.startRunUI(product.name);
  expect((await t.run(run2)).planned_batches).toBe(1);
  await t.finishRunUI(run2, "6");
  expect(completedFor(t, line)).toBe(10);
  await expectOrderUI(t, orderId, "READY");
  t.note("Continuation run used only the remainder (1 batch); Order READY after 4 + 6 = 10");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T06 Overproduction creates real Surplus", async ({ t }) => {
  const { product } = await t.standardProduced("Cookie", { a: 10000, b: 5000 }, { usableDays: 7, reusable: true });
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 5 }]);
  await t.confirmOrderUI(orderId);
  const line = await lineOf(t, orderId);
  const runId = await t.startRunUI(product.name);
  expect(await t.surplusLots(), "no Surplus from merely-expected excess before Finish").toEqual([]);
  await t.finishRunUI(runId, "8");
  expect(completedFor(t, line)).toBe(5);
  await expectOrderUI(t, orderId, "READY");
  const lots = await t.surplusLots();
  expect(lots.length).toBe(1);
  const lot = lots[0];
  expect(Number(lot.physical_quantity)).toBe(3);
  expect(lot.source_production_run_id).toBe(runId);
  expect(Number(lot.unit_cost_basis)).toBeGreaterThan(0);
  expect(lot.is_reusable).toBe(true);
  expect(lot.usable_through_date).toBe(addDays(t.today(), 7));
  await t.page.goto("/app/surplus");
  await expect(t.page.getByRole("row").filter({ hasText: product.name })).toContainText("Production");
  await t.shot(t.page, "surplus-list");
  await t.page.goto(`/app/surplus/${lot.id}/history`);
  await expect(t.page.getByText("Production output")).toBeVisible();
  await expect(t.page.getByText("+3")).toBeVisible();
  await t.shot(t.page, "surplus-history");
  t.note(
    `5 allocated to the Order (READY); 3 units of real Surplus (source Production, unit cost ${lot.unit_cost_basis}, usable through ${lot.usable_through_date}); PRODUCTION_OUTPUT history +3`,
  );
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T07 Zero-output failed batch", async ({ t }) => {
  const { product, a, b } = await t.standardProduced();
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 9 }]);
  await t.confirmOrderUI(orderId);
  const runId = await t.startRunUI(product.name);
  const before = { a: Number(await t.ingredientQty(a.id)), b: Number(await t.ingredientQty(b.id)) };
  await t.finishRunUI(runId, "0");
  await t.shot(t.page, "zero-output-finished");
  const run = await t.run(runId);
  expect(run.status).toBe("COMPLETED");
  expect(Number(run.actual_output_quantity)).toBe(0);
  expect(Number(await t.ingredientQty(a.id))).toBe(before.a - 240);
  expect(Number(await t.ingredientQty(b.id))).toBe(before.b - 120);
  for (const id of [a.id, b.id])
    expect((await t.ingredientTxns(id)).some((x) => x.transaction_type === "PRODUCTION_CONSUMPTION")).toBe(true);
  expect(run.actual_total_production_cost).not.toBeNull();
  expect(Number(run.actual_total_production_cost)).toBeGreaterThan(0);
  expect(
    Number(t.q(`select count(*) from production_run_order_allocations where production_run_id='${runId}'`)[0][0]),
  ).toBe(0);
  expect(await t.surplusLots()).toEqual([]);
  await expectOrderUI(t, orderId, "CONFIRMED");
  await expect((await planRow(t, product.name)).getByRole("cell").nth(2)).toHaveText("9");
  await t.shot(t.page, "full-demand-back-in-plan");
  t.note(
    `Zero output: run COMPLETED, cost ${run.actual_total_production_cost}, ingredients consumed (-240/-120), no allocation, no Surplus, order CONFIRMED, full demand 9 back in plan, no error`,
  );
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T08 Actual ingredient usage overrides", async ({ t }) => {
  const { product, a, b } = await t.standardProduced();
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }]);
  await t.confirmOrderUI(orderId);
  const runId = await t.startRunUI(product.name);
  await expect(t.page.getByRole("row").filter({ hasText: "Cookie Flour" })).toContainText("120");
  await expect(t.page.getByRole("row").filter({ hasText: "Cookie Sugar" })).toContainText("60");
  const before = { a: Number(await t.ingredientQty(a.id)), b: Number(await t.ingredientQty(b.id)) };
  await t.finishRunUI(runId, "6", { "Cookie Flour": "130", "Cookie Sugar": "55" });
  await t.shot(t.page, "overrides-finished");
  const run = await t.run(runId);
  const byName = Object.fromEntries((run.run_ingredients as Json[]).map((r) => [r.ingredient_name, r]));
  expect(Number(byName["Cookie Flour"].actual_quantity)).toBe(130);
  expect(Number(byName["Cookie Sugar"].actual_quantity)).toBe(55);
  expect(Number(await t.ingredientQty(a.id))).toBe(before.a - 130);
  expect(Number(await t.ingredientQty(b.id))).toBe(before.b - 55);
  const flourTxn = (await t.ingredientTxns(a.id)).find((x) => x.transaction_type === "PRODUCTION_CONSUMPTION");
  const sugarTxn = (await t.ingredientTxns(b.id)).find((x) => x.transaction_type === "PRODUCTION_CONSUMPTION");
  expect(Math.abs(Number(flourTxn!.quantity_change))).toBe(130);
  expect(Math.abs(Number(sugarTxn!.quantity_change))).toBe(55);
  // Actual cost = 130 g x $0.01 + 55 g x $0.02 = $2.40 (a fresh Business has labor rate 0).
  expect(Number(run.actual_ingredient_cost)).toBeCloseTo(2.4, 6);
  const recipe = JSON.stringify(await t.api.get(`/products/${product.id}/recipe`));
  expect(recipe).toContain("120");
  expect(recipe).toContain("60");
  expect(recipe).not.toContain("130");
  t.note("Actual 130 g/55 g used for deduction, history and cost ($2.40); Recipe still 120 g/60 g");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T09 Production may drive Ingredient inventory negative", async ({ t }) => {
  t.expectResponses("OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT");
  const { product, a } = await t.standardProduced("Cookie", { a: 100, b: 5000 });
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }]);
  const conf = await t.confirmOrderUI(orderId);
  t.note(`Confirm shortage warning acknowledged: ${conf.acknowledged} (${conf.warnings.join(" | ").slice(0, 200)})`);
  const runId = await t.startRunUI(product.name);
  await t.finishRunUI(runId, "6", { "Cookie Flour": "250" });
  await expect(t.page.getByText("negative recorded balance")).toBeVisible();
  await expect(t.page.getByRole("alert").filter({ hasText: "Cookie Flour" })).toBeVisible();
  await t.shot(t.page, "negative-balance-warning");
  expect(await t.ingredientQty(a.id)).toBe("-150.000000");
  const types = new Set((await t.ingredientTxns(a.id)).map((x) => x.transaction_type));
  expect(types.has("PRODUCTION_CONSUMPTION")).toBe(true);
  expect([...types].filter((x) => x !== "PRODUCTION_CONSUMPTION" && x !== "INITIAL_BALANCE")).toEqual([]);
  t.note(
    "Flour 100 g - 250 g = -150 g (not clamped); consumption recorded; only INITIAL_BALANCE + PRODUCTION_CONSUMPTION history (no fabricated restock)",
  );
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});
