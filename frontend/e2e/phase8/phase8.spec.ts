// Phase 8 Automated Manual-Acceptance Pass — Tests 1–16.
// Browser/UI -> React -> FastAPI -> services -> PostgreSQL. Each test owns a fresh tenant.

import { expect, test } from "./helpers";

test("T01 Normal production plan and Start — no Ingredient consumption", async ({ t }) => {
  const { product, a, b } = await t.standardProduced();
  const before = { a: await t.ingredientQty(a.id), b: await t.ingredientQty(b.id) };
  t.note(`Seeded ingredient balances A=${before.a} B=${before.b}`);

  const orderId = await t.createOrderUI([{ kind: "quantity", product: product.name, qty: 9 }]);
  t.entityIds.order = orderId;
  await t.confirmOrderUI(orderId);
  expect((await t.order(orderId)).status).toBe("CONFIRMED");

  await t.page.goto("/app/production");
  const row = t.page.getByRole("row").filter({ hasText: product.name });
  await expect(row).toBeVisible();
  await expect(row.getByRole("cell").nth(2)).toHaveText("9");
  await expect(row.getByRole("cell").nth(3)).toHaveText("2");
  await expect(row.getByRole("cell").nth(4)).toHaveText("12");
  t.note("Plan shows demand 9, recommended batches 2, expected output 12");
  await t.shot(t.page, "production-plan");

  const runId = await t.startRunUI(product.name);
  t.entityIds.run = runId;
  await expect(t.page.getByText("In Production")).toBeVisible();
  await t.shot(t.page, "run-started");
  const run = await t.run(runId);
  expect(run.status).toBe("IN_PRODUCTION");
  expect(run.planned_batches).toBe(2);

  expect(await t.ingredientQty(a.id)).toBe(before.a);
  expect(await t.ingredientQty(b.id)).toBe(before.b);
  const consumption = (await t.ingredientTxns(a.id)).filter((x) => x.transaction_type === "PRODUCTION_CONSUMPTION");
  expect(consumption).toEqual([]);
  t.note("Ingredient physical balances exactly unchanged after Start; no consumption transactions");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});
