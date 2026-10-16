// Phase 9 Acceptance — Shopping List and Dashboard (Acceptance Matrix E1–E4).
// Browser/UI -> React -> FastAPI -> services -> PostgreSQL. Each test owns a fresh tenant
// (see ../phase8/helpers.ts, reused unchanged apart from its evidence-directory override).

import { Tenant, expect, test } from "../phase8/helpers";

test("T91 Shortage workflow — Dashboard, Shopping List, Production Plan and Inventory agree; restock resolves it", async ({
  t,
}) => {
  // Confirming with a shortage first returns the structured acknowledgement-required 422.
  t.expectResponses("OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT");
  // Flour is deliberately scarce: 2 batches x 120 g = 240 g needed against 100 g on hand.
  const { product, a } = await t.standardProduced("Cookie", { a: 100, b: 5000 });
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 9 }], {
    offsetDays: 1,
  });
  t.entityIds.order = orderId;
  await t.confirmOrderUI(orderId);
  expect((await t.order(orderId)).status).toBe("CONFIRMED");

  // Dashboard: one aggregated, actionable shortage alert.
  await t.page.goto("/app/dashboard");
  const alert = t.page.getByTestId("alert-INGREDIENT_SHORTAGE");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("Cookie Flour");
  await expect(alert).toContainText("short 140 g");
  await t.shot(t.page, "dashboard-shortage");

  // Shopping List: the same 140 g, with a contributing-demand drilldown.
  await alert.getByRole("link", { name: "Open Shopping List" }).click();
  await expect(t.page.getByRole("heading", { name: "Shopping List" })).toBeVisible();
  const row = t.page.getByRole("row").filter({ hasText: "Cookie Flour" });
  await expect(row).toContainText("Short 140 g");
  await row.getByRole("button", { name: /show contributing demand/i }).click();
  const drilldown = t.page.getByTestId(`drilldown-${a.id}`);
  await expect(drilldown).toContainText(product.name);
  await expect(drilldown.getByRole("link", { name: /^ORD-/ })).toBeVisible();
  await t.shot(t.page, "shopping-list-shortage");

  // Production Plan shows the identical shortage and the affected order.
  await t.page.goto("/app/production");
  const planRow = t.page.getByRole("row").filter({ hasText: product.name });
  await expect(planRow).toContainText("Cookie Flour: short 140 g");
  await expect(planRow.getByRole("link", { name: /^ORD-/ })).toBeVisible();

  // Inventory: Physical / Reserved / Available are distinct and derived.
  await t.page.goto(`/app/inventory/ingredients/${a.id}`);
  await expect(t.page.getByText("Reserved for confirmed demand")).toBeVisible();
  const detail = t.page.getByRole("main");
  await expect(detail).toContainText("240 g");
  await expect(detail).toContainText("-140 g");
  await expect(detail).toContainText("reservations exceed stock");

  // Restock (real endpoint) -> every screen resolves on its next load, no recalculation step.
  const current = await t.api.get(`/ingredients/${a.id}`);
  await t.api.post(`/ingredients/${a.id}/inventory/restock`, {
    version: current.version,
    quantity: "500",
    unit: "g",
    unit_cost: "0.01",
  });
  await t.page.goto("/app/production/shopping-list");
  await expect(t.page.getByTestId("no-shortages")).toContainText(/nothing to buy/i);
  await t.page.goto("/app/dashboard");
  await expect(t.page.getByTestId("alert-INGREDIENT_SHORTAGE")).toHaveCount(0);
  await t.page.goto(`/app/inventory/ingredients/${a.id}`);
  await expect(t.page.getByRole("main")).toContainText("360 g");
  await t.shot(t.page, "after-restock");

  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T92 Dashboard — fulfillment risk, missing time, due-today production, active run, upcoming", async ({
  t,
}) => {
  t.expectResponses("OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT");
  const { product } = await t.standardProduced("Roll", { a: 100000, b: 100000 });
  const today = t.today();

  // 00:01 today: its deadline (minus the warning window) has always already begun -> at risk.
  const timed = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }], {
    offsetDays: 0,
    time: "00:01",
  });
  const untimed = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }], {
    offsetDays: 0,
    time: "",
  });
  const later = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }], {
    offsetDays: 2,
    time: "10:00",
  });
  for (const id of [timed, untimed, later]) {
    await t.confirmOrderUI(id);
  }
  t.entityIds.timed = timed;
  t.entityIds.untimed = untimed;
  t.entityIds.later = later;

  await t.page.goto("/app/dashboard");
  await expect(t.page.getByRole("heading", { name: "Attention Required" })).toBeVisible();
  const risk = t.page.getByTestId("alert-FULFILLMENT_AT_RISK");
  await expect(risk).toContainText("1 order is at risk");
  const missing = t.page.getByTestId("alert-MISSING_FULFILLMENT_TIME");
  await expect(missing).toContainText("1 upcoming order has no fulfillment time");

  // Today's Fulfillments: the timed order first, the untimed one marked "No time set".
  const fulfillments = t.page.getByRole("heading", { name: "Today's Fulfillments" }).locator("..");
  const rows = fulfillments.getByRole("row");
  await expect(rows).toHaveCount(3); // header + 2 orders
  await expect(rows.nth(1)).toContainText("12:01 AM");
  await expect(rows.nth(1)).toContainText("At risk");
  await expect(rows.nth(2)).toContainText("No time set");
  await expect(rows.nth(1)).toContainText("Confirmed — not ready");

  // Today's Production: both today's orders aggregate into one due-today requirement; with
  // the fulfillment time absent on one and no recipe elapsed time, no start is invented.
  const production = t.page.getByRole("heading", { name: "Today's Production" }).locator("..");
  await expect(production).toContainText(product.name);
  await expect(production).toContainText("Due today");
  await expect(production).toContainText("No suggested start time");

  // Snapshot: operational counts only, value explicitly labeled as not revenue.
  const snapshot = t.page.getByRole("heading", { name: "Today's Snapshot" }).locator("..");
  await expect(snapshot).toContainText("Scheduled order value today (not revenue)");
  await expect(snapshot.locator("dt", { hasText: "Orders due today" }).locator("..")).toContainText(
    "2",
  );

  // Upcoming: the +2-day order.
  const upcoming = t.page.getByRole("heading", { name: "Upcoming" }).locator("..");
  await expect(upcoming.getByRole("link", { name: /^ORD-/ })).toHaveCount(1);
  await t.shot(t.page, "dashboard-sections");

  // Start the due-today requirement for real: it moves from Today's Production to Active.
  const runId = await t.startRunUI(product.name, { demandDate: today });
  t.entityIds.run = runId;
  await t.page.goto("/app/dashboard");
  await expect(t.page.getByRole("heading", { name: "Active Production" })).toBeVisible();
  await expect(
    t.page.getByRole("heading", { name: "Active Production" }).locator(".."),
  ).toContainText(product.name);
  await expect(t.page.getByRole("heading", { name: "Today's Production" })).toHaveCount(0);
  await t.shot(t.page, "dashboard-active-run");

  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T93 Tenant isolation — another business never sees this tenant's Dashboard, Shopping List or requirements", async ({
  t,
  browser,
}, testInfo) => {
  t.expectResponses("OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT");
  const { product, a } = await t.standardProduced("Alpha", { a: 50, b: 50 });
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 9 }], {
    offsetDays: 1,
  });
  await t.confirmOrderUI(orderId);
  const requirementId = (await t.requirements())[0].id as string;

  // Tenant A really has content.
  await t.page.goto("/app/dashboard");
  await expect(t.page.getByTestId("alert-INGREDIENT_SHORTAGE")).toBeVisible();

  const other = await Tenant.create(browser, testInfo);
  try {
    // B's own screens are empty.
    await other.page.goto("/app/dashboard");
    await expect(other.page.getByTestId("dashboard-empty")).toBeVisible();
    await other.page.goto("/app/production/shopping-list");
    await expect(other.page.getByTestId("no-shortages")).toContainText(/no ingredient demand/i);

    // B's raw API payloads contain none of A's identifiers or names.
    const dashboard = JSON.stringify(await other.api.get("/dashboard"));
    const shopping = JSON.stringify(await other.api.get("/shopping-list?horizon_days=7"));
    for (const blob of [dashboard, shopping]) {
      expect(blob).not.toContain(product.name);
      expect(blob).not.toContain(a.id);
      expect(blob).not.toContain(orderId);
      expect(blob).not.toContain(t.businessName);
    }
    // A forged business_id is ignored; A's requirement is unavailable (404) to B.
    const forged = JSON.stringify(await other.api.get(`/dashboard?business_id=${t.businessId}`));
    expect(forged).not.toContain(product.name);
    other.expectResponses("NOT_FOUND", "HTTP_ERROR");
    const foreign = await other.api.send("post", "/production-runs", {
      production_requirement_id: requirementId,
    });
    expect(foreign.status).toBe(404);
  } finally {
    await other.finish();
  }

  t.expectResponses("NOT_FOUND", "HTTP_ERROR");
  t.assertBrowserHealthy();
});
