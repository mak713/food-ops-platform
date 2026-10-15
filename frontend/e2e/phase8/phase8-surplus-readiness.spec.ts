// Phase 8 Automated Manual-Acceptance Pass — Tests 10–16 (Surplus, readiness, mixed Orders).

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

test("T10 Existing Surplus makes the Order READY immediately", async ({ t }) => {
  const { product } = await t.standardProduced();
  await t.recordSurplusUI(product.name, "5");
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 5 }], { offsetDays: 3 });
  await t.confirmOrderUI(orderId);
  await expectOrderUI(t, orderId, "READY");
  await t.shot(t.page, "ready-on-confirmation");
  const line = await lineOf(t, orderId);
  expect(surplusFor(t, line)).toBe(5);
  await t.page.goto("/app/production");
  await expect(t.page.getByText("Nothing needs production right now.")).toBeVisible();
  expect(
    Number(t.q(`select coalesce(sum(production_demand_quantity),0) from production_requirements where business_id=$BIZ`)[0][0]),
  ).toBe(0);
  expect(Number(t.q(`select count(*) from ingredient_reservations where business_id=$BIZ`)[0][0])).toBe(0);
  expect(Number(t.q(`select count(*) from production_runs where business_id=$BIZ`)[0][0])).toBe(0);
  t.note("READY during confirmation via 5 Surplus; no run, no phantom production demand, no ingredient reservations");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T11A Consequential negative Surplus adjustment requires acknowledgment", async ({ t }) => {
  t.expectResponses("SURPLUS_ADJUSTMENT_REDUCES_ALLOCATION_REQUIRES_ACKNOWLEDGMENT");
  const { product } = await t.standardProduced();
  await t.recordSurplusUI(product.name, "5");
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 5 }], { offsetDays: 3 });
  await t.confirmOrderUI(orderId);
  await expectOrderUI(t, orderId, "READY");
  const line = await lineOf(t, orderId);
  const lot = (await t.surplusLots())[0];

  await t.adjustSurplusUI(lot.id, "1");
  await expect(t.page.getByText("This will demote Ready orders")).toBeVisible();
  await expect(t.page.getByRole("button", { name: "Adjust anyway" })).toBeVisible();
  await t.shot(t.page, "adjust-consequence-warning");
  expect(Number((await t.surplusLots())[0].physical_quantity)).toBe(5);
  expect(surplusFor(t, line)).toBe(5);
  t.note("Initial reduction blocked behind the allocation-loss warning; nothing mutated");

  await t.page.getByRole("button", { name: "Adjust anyway" }).click();
  await t.page.waitForURL(/\/app\/surplus$/);
  expect(Number((await t.surplusLots())[0].physical_quantity)).toBe(4);
  expect(surplusFor(t, line)).toBe(4);
  await expectOrderUI(t, orderId, "CONFIRMED");
  await expect(t.page.getByText("previously Ready")).toBeVisible();
  await t.shot(t.page, "order-demoted");
  t.note("After acknowledgment: physical 4, allocation 4, Order demoted READY -> CONFIRMED");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T11B Active-run floor is a hard block", async ({ t }) => {
  t.expectResponses("SURPLUS_ADJUSTMENT_PRODUCTION_LOCKED");
  const { product } = await t.standardProduced();
  await t.recordSurplusUI(product.name, "3");
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }]);
  await t.confirmOrderUI(orderId);
  const line = await lineOf(t, orderId);
  expect(surplusFor(t, line)).toBe(3);
  const runId = await t.startRunUI(product.name);
  const lot = (await t.surplusLots())[0];
  const orderBefore = await t.order(orderId);

  await t.adjustSurplusUI(lot.id, "1");
  await expect(t.page.getByRole("alert").filter({ hasText: /production run/i })).toBeVisible();
  await expect(t.page.getByRole("button", { name: "Adjust anyway" })).toHaveCount(0);
  await t.shot(t.page, "production-locked-rejection");
  expect(Number((await t.surplusLots())[0].physical_quantity)).toBe(3);
  expect(surplusFor(t, line)).toBe(3);
  expect((await t.order(orderId)).status).toBe(orderBefore.status);
  t.note("Reduction rejected as production-locked; no 'Adjust anyway'; physical 3, allocation 3, Order unchanged");
  await t.cancelRunUI(runId);
  t.note("Run canceled only after the assertions were recorded");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T12 Purchased-only Order READY without physical consumption", async ({ t }) => {
  const pp = await t.purchasedProduct("Gift Tin", 10);
  const orderId = await t.createOrderUI([{ kind: "quantity", product: pp.name, qty: 2 }]);
  await t.confirmOrderUI(orderId);
  await expectOrderUI(t, orderId, "READY");
  await t.shot(t.page, "purchased-ready");
  const inv = await t.api.get(`/products/${pp.id}/purchased-inventory`);
  expect(Number(inv.physical_quantity)).toBe(10);
  expect(
    Number(t.q(`select coalesce(sum(quantity),0) from purchased_product_reservations where business_id=$BIZ`)[0][0]),
  ).toBe(2);
  expect(Number(t.q(`select count(*) from production_requirements where product_id='${pp.id}'`)[0][0])).toBe(0);
  expect(Number(t.q(`select count(*) from recipes where product_id='${pp.id}'`)[0][0])).toBe(0);
  t.note("READY; reservation 2; physical Purchased inventory still exactly 10; no Recipe/ProductionRequirement");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T13 Custom Item manual satisfaction", async ({ t }) => {
  t.expectResponses("LINE_NOT_MANUAL_FULFILLMENT");
  const orderId = await t.createOrderUI([{ kind: "custom-item", description: "Hand-lettered card" }]);
  await t.confirmOrderUI(orderId);
  const page = t.page;
  await expect(page.getByText("Not satisfied")).toBeVisible();
  await expectOrderUI(t, orderId, "CONFIRMED");
  await t.shot(page, "unsatisfied");

  await page.getByRole("button", { name: "Mark satisfied" }).click();
  await expect(page.getByText("Satisfied", { exact: true })).toBeVisible();
  await expect(page.getByText("READY", { exact: true }).first()).toBeVisible();
  expect((await t.order(orderId)).status).toBe("READY");
  await t.shot(page, "satisfied-ready");

  await page.getByRole("button", { name: "Mark unsatisfied" }).click();
  await expect(page.getByText("Not satisfied")).toBeVisible();
  await expect(page.getByText("CONFIRMED", { exact: true }).first()).toBeVisible();
  expect((await t.order(orderId)).status).toBe("CONFIRMED");
  t.note("Mark satisfied -> READY; Mark unsatisfied -> CONFIRMED");

  // A non-Custom line: the UI exposes no control, and the endpoint must reject it.
  const pp = await t.purchasedProduct("Ribbon", 10);
  const other = await t.createOrderUI([{ kind: "quantity", product: pp.name, qty: 1 }]);
  await t.confirmOrderUI(other);
  await page.goto(`/app/orders/${other}`);
  await expect(page.getByRole("button", { name: /Mark (un)?satisfied/ })).toHaveCount(0);
  const ord = await t.order(other);
  const r = await t.api.send("post", `/orders/${other}/lines/${ord.lines[0].id}/manual-fulfillment`, {
    version: ord.version,
    satisfied: true,
  });
  expect(r.status).toBe(422);
  expect((r.body as Json).error.code).toBe("LINE_NOT_MANUAL_FULFILLMENT");
  t.note("Non-Custom line: no UI control; direct API attempt rejected 422 LINE_NOT_MANUAL_FULFILLMENT");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T14 Mixed Order requires all categories", async ({ t }) => {
  const { product } = await t.standardProduced();
  const pp = await t.purchasedProduct("Gift Tin", 10);
  const orderId = await t.createOrderUI([
    { kind: "quantity", product: product.name, qty: 6 },
    { kind: "quantity", product: pp.name, qty: 2 },
    { kind: "custom-item", description: "Personal note card" },
  ]);
  await t.confirmOrderUI(orderId);
  const page = t.page;
  const ord = await t.order(orderId);
  const producedLine = (ord.lines as Json[]).find((l) => l.product_id === product.id)!.id as string;
  await expectOrderUI(t, orderId, "CONFIRMED");
  expect(
    Number(t.q(`select coalesce(sum(quantity),0) from purchased_product_reservations where business_id=$BIZ`)[0][0]),
  ).toBe(2);
  expect(completedFor(t, producedLine) + surplusFor(t, producedLine)).toBe(0);
  await expect(page.getByText("Not satisfied")).toBeVisible();
  await t.shot(page, "confirmed-mixed");
  t.note("Confirmed: Purchased reserved (2), Produced not fulfilled, Custom unsatisfied -> CONFIRMED");

  await page.getByRole("button", { name: "Mark satisfied" }).click();
  await expect(page.getByText("Satisfied", { exact: true })).toBeVisible();
  expect((await t.order(orderId)).status).toBe("CONFIRMED");
  await t.shot(page, "custom-satisfied-still-confirmed");
  t.note("Custom Item satisfied; Order still CONFIRMED because production remains");

  const runId = await t.startRunUI(product.name);
  await t.finishRunUI(runId, "6");
  expect(completedFor(t, producedLine)).toBe(6);
  await expectOrderUI(t, orderId, "READY");
  await t.shot(page, "ready-all-categories");
  t.note("After Finish(6): Produced, Purchased and Custom all satisfied -> READY");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test.describe("T15 group", () => {
  // The browser's own timezone is deliberately far from the Business's (UTC+14 vs
  // America/New_York) to prove the client never decides Surplus eligibility.
  test.use({ tenantOptions: { timezoneId: "Pacific/Kiritimati" } });

  test("T15 Usable-through eligibility uses the Business-local calendar date", async ({ t }) => {
    const { product } = await t.standardProduced();
    const me = await t.api.get("/auth/me");
    expect(me.business.timezone).toBe("America/New_York");
    const fulfil = addDays(t.today(), 10);
    await t.recordSurplusUI(product.name, "5", { usableThrough: addDays(fulfil, -1) });
    await t.recordSurplusUI(product.name, "5", { usableThrough: fulfil });
    const lots = await t.surplusLots();
    const lotA = lots.find((l) => l.usable_through_date === addDays(fulfil, -1))!;
    const lotB = lots.find((l) => l.usable_through_date === fulfil)!;
    const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 5 }], {
      offsetDays: 10,
    });
    await t.confirmOrderUI(orderId);
    await t.page.goto("/app/surplus");
    await t.shot(t.page, "both-lots-recorded");
    const alloc = (lot: Json) =>
      Number(
        t.q(`select coalesce(sum(quantity),0) from surplus_allocations where surplus_inventory_id='${lot.id}'`)[0][0],
      );
    expect(alloc(lotA), "Lot A (usable through the day BEFORE fulfillment) is ineligible").toBe(0);
    expect(alloc(lotB), "Lot B is eligible and selected").toBe(5);
    const after = await t.surplusLots();
    expect(after.find((l) => l.id === lotA.id)!.physical_quantity).toBe(lotA.physical_quantity);
    await expectOrderUI(t, orderId, "READY");
    t.note(
      `Business tz ${me.business.timezone}, browser tz Pacific/Kiritimati; fulfillment ${fulfil}: Lot A (through ${addDays(fulfil, -1)}) unused but still recorded, Lot B (through ${fulfil}) allocated 5, Order READY`,
    );
    await t.assertGlobalInvariants();
    t.assertBrowserHealthy();
  });
});

test("T16 READY Order while its own Run remains IN_PRODUCTION", async ({ t }) => {
  t.expectResponses("SURPLUS_ADJUSTMENT_PRODUCTION_LOCKED");
  const { product } = await t.standardProduced();
  const page = t.page;

  // Step A — B partially covered by Surplus; B's run started and left IN_PRODUCTION.
  await t.recordSurplusUI(product.name, "3");
  const dB = addDays(t.today(), 2);
  const dA = addDays(t.today(), 4);
  const orderB = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }], { offsetDays: 2 });
  await t.confirmOrderUI(orderB);
  const lineB = await lineOf(t, orderB);
  expect(surplusFor(t, lineB)).toBe(3);
  await expect((await planRow(t, product.name, dB)).getByRole("cell").nth(2)).toHaveText("3");
  const runB = await t.startRunUI(product.name, { demandDate: dB });
  t.entityIds.runB = runB;
  const lot1 = (await t.surplusLots())[0];
  const reqB = (await t.run(runB)).source_production_requirement_id as string;
  const allocB = t.q(`select id from surplus_allocations where order_line_id='${lineB}'`);
  expect(allocB.length).toBe(1);
  const reservationsB = t.q(
    `select id from ingredient_reservations where production_requirement_id='${reqB}' order by id`,
  );
  t.note("B (6): 3 Surplus + 3 production; Run B started and left IN_PRODUCTION");

  // Step B — another run completes B's remaining demand.
  const orderA = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 3 }], { offsetDays: 4 });
  await t.confirmOrderUI(orderA);
  const lineA = await lineOf(t, orderA);
  const runA = await t.startRunUI(product.name, { demandDate: dA });
  await t.finishRunUI(runA, "3");
  expect(completedFor(t, lineB), "earliest-deadline B receives the completed output").toBe(3);
  await expectOrderUI(t, orderB, "READY");
  await t.shot(page, "B-ready-run-still-active");
  expect((await t.run(runB)).status).toBe("IN_PRODUCTION");
  expect(surplusFor(t, lineB)).toBe(3);
  expect(completedFor(t, lineA)).toBe(0);
  t.note("B READY (3 completed + 3 Surplus = 6) while Run B is still IN_PRODUCTION — legitimate overlap");

  // Step C — another recalculation: Order C plus a new reusable lot.
  const orderC = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 2 }], { offsetDays: 6 });
  await t.confirmOrderUI(orderC);
  await t.recordSurplusUI(product.name, "5");
  const lineC = await lineOf(t, orderC);
  await expectOrderUI(t, orderB, "READY");
  expect((await t.run(runB)).status).toBe("IN_PRODUCTION");
  expect(t.q(`select id from production_requirements where id='${reqB}'`).length).toBe(1);
  expect(t.q(`select id from surplus_allocations where order_line_id='${lineB}'`)).toEqual(allocB);
  expect(
    t.q(`select id from ingredient_reservations where production_requirement_id='${reqB}' order by id`),
  ).toEqual(reservationsB);
  expect(
    t.q(`select production_requirement_id from production_requirement_orders where order_line_id='${lineB}'`),
  ).toEqual([[reqB]]);
  const lot1Alloc = Number(
    t.q(`select coalesce(sum(quantity),0) from surplus_allocations where surplus_inventory_id='${lot1.id}'`)[0][0],
  );
  expect(lot1Alloc).toBe(3);
  expect(surplusFor(t, lineA)).toBe(3);
  expect(surplusFor(t, lineC)).toBe(2);
  await expectOrderUI(t, orderA, "READY");
  await expectOrderUI(t, orderC, "READY");
  await t.shot(page, "after-recalculation");
  t.note(
    "After Order C + new lot: B READY/run IN_PRODUCTION; requirement, ONE Surplus claim (same id, not cloned), reservations unchanged; new lot flows to A(3) and C(2)",
  );

  // Step D — the active-run hard floor is stronger than READY protection.
  await t.adjustSurplusUI(lot1.id, "1");
  await expect(page.getByRole("alert").filter({ hasText: /production run/i })).toBeVisible();
  await expect(page.getByRole("button", { name: "Adjust anyway" })).toHaveCount(0);
  await t.shot(page, "hard-floor-rejection");
  const lots = await t.surplusLots();
  expect(Number(lots.find((l) => l.id === lot1.id)!.physical_quantity)).toBe(3);
  expect(t.q(`select id from surplus_allocations where order_line_id='${lineB}'`)).toEqual(allocB);
  expect((await t.order(orderB)).status).toBe("READY");
  expect((await t.run(runB)).status).toBe("IN_PRODUCTION");
  t.note(
    "Reduction of B's protected lot rejected (production-locked), no escape hatch; quantity, allocation, READY and run unchanged",
  );
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});
