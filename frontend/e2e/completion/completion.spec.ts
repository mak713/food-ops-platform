// Order Completion / Fulfillment acceptance. Browser/UI -> React -> FastAPI -> services ->
// PostgreSQL. Each test owns a fresh tenant (see ../phase8/helpers.ts, reused unchanged
// apart from its evidence-directory override).

import { Tenant, expect, test } from "../phase8/helpers";

const ACK = "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT";

test("T101 Purchased order: confirm -> Ready -> Complete deducts stock at WAC and records history", async ({
  t,
}) => {
  t.expectResponses(ACK);
  const juice = await t.purchasedProduct("Lemonade", 10, 1.5);
  const orderId = await t.createOrderUI([{ kind: "quantity", product: juice.name, qty: 4 }], {
    offsetDays: 0,
    time: "23:30",
  });
  t.entityIds.order = orderId;
  await t.confirmOrderUI(orderId);
  expect(await t.orderStatusBadge(orderId)).toBe("READY");

  // The order is on today's Dashboard fulfillment list while Ready.
  await t.page.goto("/app/dashboard");
  await expect(
    t.page.getByRole("heading", { name: "Today's Fulfillments" }).locator(".."),
  ).toContainText("Ready");

  await t.page.goto(`/app/orders/${orderId}`);
  await t.page.getByRole("button", { name: "Complete Order" }).click();
  const dialog = t.page.getByRole("alertdialog");
  await expect(dialog).toContainText("cannot be undone");
  await expect(dialog).toContainText("Payment is not required");
  await dialog.getByRole("button", { name: "Complete order" }).click();

  const panel = t.page.getByTestId("completed-panel");
  await expect(panel).toBeVisible();
  const cost = t.page.getByTestId("historical-cost");
  await expect(cost).toContainText("Purchased goods");
  await expect(cost.getByText("Purchased goods").locator("xpath=following-sibling::dd")).toHaveText(
    "$6.00",
  );
  await expect(cost.getByText("Surplus used").locator("xpath=following-sibling::dd")).toHaveText(
    "$0.00",
  );
  await expect(
    cost.getByText(/Total historical direct cost/).locator("xpath=following-sibling::dd"),
  ).toHaveText("$6.00");
  await t.shot(t.page, "completed-purchased");

  // Physical truth + history through the real API.
  const inventory = await t.api.get(`/products/${juice.id}/purchased-inventory`);
  expect(Number(inventory.physical_quantity)).toBe(6);
  expect(Number(inventory.weighted_average_unit_cost)).toBe(1.5);
  const txns = (await t.api.get(`/products/${juice.id}/purchased-inventory/transactions`)).items;
  const fulfillment = txns.filter(
    (x: { transaction_type: string }) => x.transaction_type === "ORDER_FULFILLMENT",
  );
  expect(fulfillment).toHaveLength(1);
  expect(Number(fulfillment[0].quantity_change)).toBe(-4);

  // Completed orders leave the operational views.
  await t.page.goto("/app/dashboard");
  await expect(t.page.getByRole("heading", { name: "Today's Fulfillments" })).toHaveCount(0);

  // Payment may still be recorded after completion.
  await t.page.goto(`/app/orders/${orderId}`);
  await expect(t.page.getByTestId("completed-panel")).toBeVisible();
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T102 Surplus + production chain: Completion consumes Surplus, leaves PRODUCTION history untouched", async ({
  t,
}) => {
  t.expectResponses(ACK);
  const { product } = await t.standardProduced(
    "Chain",
    { a: 100000, b: 100000 },
    { reusable: true },
  );
  await t.recordSurplusUI(product.name, "4", { unitCost: "0.50" });
  // 6 units: 4 come from the Surplus lot, 2 must be produced (one 6-unit batch).
  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 6 }]);
  t.entityIds.order = orderId;
  await t.confirmOrderUI(orderId);

  const runId = await t.startRunUI(product.name);
  await t.finishRunUI(runId, "6");
  expect(await t.orderStatusBadge(orderId)).toBe("READY");

  const readyBefore = await t.order(orderId);
  const productionRows = t.q(
    `select id, amount from order_cost_allocations where order_id='${orderId}' and cost_type='PRODUCTION' order by id`,
  );
  expect(productionRows).toHaveLength(1);

  await t.page.goto(`/app/orders/${orderId}`);
  await t.page.getByRole("button", { name: "Complete Order" }).click();
  await t.page.getByRole("alertdialog").getByRole("button", { name: "Complete order" }).click();
  await expect(t.page.getByTestId("completed-panel")).toBeVisible();
  await t.shot(t.page, "completed-chain");

  const done = await t.order(orderId);
  expect(done.status).toBe("COMPLETED");
  expect(done.version).toBeGreaterThan(readyBefore.version);
  const cost = done.historical_cost;
  expect(cost.by_type.SURPLUS).toBe("2.00"); // 4 x 0.50
  const sum = Object.values(cost.by_type as Record<string, string>).reduce(
    (a, b) => a + Number(b),
    0,
  );
  expect(Number(cost.total)).toBeCloseTo(sum, 2);

  // PRODUCTION history is byte-for-byte unchanged.
  expect(
    t.q(
      `select id, amount from order_cost_allocations where order_id='${orderId}' and cost_type='PRODUCTION' order by id`,
    ),
  ).toEqual(productionRows);

  // The Surplus lot was physically consumed and its allocation removed.
  const lots = await t.surplusLots();
  const seeded = lots.find(
    (l: { source_production_run_id: string | null }) => l.source_production_run_id === null,
  );
  expect(Number(seeded!.physical_quantity)).toBe(0);
  expect(t.q(`select count(*) from surplus_allocations where order_id='${orderId}'`)[0][0]).toBe(
    "0",
  );
  const txCount = t.q(
    `select count(*) from surplus_transactions where order_id='${orderId}' and transaction_type='ORDER_FULFILLMENT'`,
  )[0][0];
  expect(txCount).toBe("1");

  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T103 Double-submit and stale tab: exactly one fulfillment, the stale tab is told to refresh", async ({
  t,
}) => {
  t.expectResponses(ACK, "STALE_VERSION", "ORDER_NOT_COMPLETABLE");
  const juice = await t.purchasedProduct("Soda", 10, 2);
  const orderId = await t.createOrderUI([{ kind: "quantity", product: juice.name, qty: 3 }]);
  await t.confirmOrderUI(orderId);
  expect(await t.orderStatusBadge(orderId)).toBe("READY");

  // A second tab loads the order (READY, current version) before the first completes it.
  const staleTab = await t.newPage();
  await staleTab.goto(`/app/orders/${orderId}`);
  await expect(staleTab.getByRole("button", { name: "Complete Order" })).toBeVisible();

  await t.page.goto(`/app/orders/${orderId}`);
  await t.page.getByRole("button", { name: "Complete Order" }).click();
  await t.page.getByRole("alertdialog").getByRole("button", { name: "Complete order" }).click();
  await expect(t.page.getByTestId("completed-panel")).toBeVisible();

  await staleTab.getByRole("button", { name: "Complete Order" }).click();
  await staleTab.getByRole("alertdialog").getByRole("button", { name: "Complete order" }).click();
  await expect(staleTab.getByRole("button", { name: "Refresh" })).toBeVisible();
  await staleTab.getByRole("button", { name: "Refresh" }).click();
  await expect(staleTab.getByTestId("completed-panel")).toBeVisible();

  const inventory = await t.api.get(`/products/${juice.id}/purchased-inventory`);
  expect(Number(inventory.physical_quantity)).toBe(7); // deducted once
  expect(
    t.q(
      `select count(*) from order_cost_allocations where order_id='${orderId}' and cost_type='PURCHASED_GOOD'`,
    )[0][0],
  ).toBe("1");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T104 Tenant isolation: another business cannot complete this tenant's order", async ({
  t,
  browser,
}, testInfo) => {
  t.expectResponses(ACK);
  const juice = await t.purchasedProduct("Tea", 5, 1);
  const orderId = await t.createOrderUI([{ kind: "quantity", product: juice.name, qty: 2 }]);
  await t.confirmOrderUI(orderId);
  expect(await t.orderStatusBadge(orderId)).toBe("READY");
  const version = (await t.order(orderId)).version;

  const other = await Tenant.create(browser, testInfo);
  try {
    other.expectResponses("NOT_FOUND", "HTTP_ERROR");
    const foreign = await other.api.send("post", `/orders/${orderId}/complete`, { version });
    expect(foreign.status).toBe(404);
    expect(JSON.stringify(foreign.body)).not.toContain("Tea");
  } finally {
    await other.finish();
  }
  expect((await t.order(orderId)).status).toBe("READY");
  expect(
    Number((await t.api.get(`/products/${juice.id}/purchased-inventory`)).physical_quantity),
  ).toBe(5);
  t.assertBrowserHealthy();
});
