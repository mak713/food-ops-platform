// Phase 10 Acceptance — historical Analytics. Browser/UI -> React -> FastAPI -> services ->
// PostgreSQL. Each test owns a fresh tenant (see ../phase8/helpers.ts, reused unchanged apart
// from its evidence-directory override). Completed Orders are produced through the real
// Order lifecycle (create -> confirm -> complete) with real Purchased inventory, so Direct
// Cost is the genuine immutable ledger written by Completion.
//
// Reference scenario (all Completed "today", Business-local), hand-computed:
//   S1 Alice PICKUP   Six-pack x2 (12 units, $20.00) + tax $1.00 -> total $21.00,
//                     cost 12 x $1.50 WAC + 2 x $0.30 packaging = $18.60, paid $10.00
//   S2 Alice DELIVERY 4 units custom quantity $20.00, packaging $0.50 -> $20.00,
//                     cost 4 x $1.50 + $0.50 = $6.50, paid in full
//   S3 Guest PICKUP   custom item $15.00 with $4.00 estimate -> $15.00, cost $4.00, unpaid
//   S4 Alice          confirmed then canceled (Canceled count only)
//   Revenue $56.00 | Direct Cost $29.10 | Contribution $26.90 | Margin 48.0% | 3 Orders | AOV $18.67

import { Tenant, addDays, expect, test } from "../phase8/helpers";

const RANGE = "range=LAST_7_DAYS";

async function seed(t: Tenant) {
  const lemonade = await t.purchasedProduct("Lemonade", 60, 1.5);
  const sixPack = await t.api.post(`/products/${lemonade.id}/selling-options`, {
    name: "Six-pack",
    quantity_units: "6",
    price: "10.00",
    packaging_cost: "0.30",
  });
  const alice = await t.api.post("/customers", { name: "Alice Analytics" });
  const date = addDays(t.today(), 1);

  const create = async (body: Record<string, unknown>) => {
    const order = await t.api.post("/orders", {
      fulfillment_date: date,
      fulfillment_time: "10:00:00",
      ...body,
    });
    return order as { id: string; version: number; order_number: string };
  };
  const confirm = async (order: { id: string; version: number }) => {
    let r = await t.api.send("post", `/orders/${order.id}/confirm`, {
      version: order.version,
      acknowledged_warning_fingerprints: [],
    });
    if (!r.ok) {
      const fingerprints = (
        (r.body as { error?: { issues?: { details?: { fingerprint?: string } }[] } }).error
          ?.issues ?? []
      )
        .map((i) => i.details?.fingerprint)
        .filter(Boolean);
      r = await t.api.send("post", `/orders/${order.id}/confirm`, {
        version: order.version,
        acknowledged_warning_fingerprints: fingerprints,
      });
    }
    expect(r.ok, JSON.stringify(r.body)).toBe(true);
    return r.body as { id: string; version: number; status: string };
  };
  const complete = async (order: { id: string; version: number }) => {
    const ready = await confirm(order);
    expect(ready.status).toBe("READY");
    const done = await t.api.post(`/orders/${order.id}/complete`, { version: ready.version });
    expect((done as { status: string }).status).toBe("COMPLETED");
  };

  const s1 = await create({
    customer_id: alice.id,
    fulfillment_method: "PICKUP",
    manual_tax: "1.00",
    lines: [
      {
        line_type: "STANDARD_OPTION",
        product_id: lemonade.id,
        selling_option_id: sixPack.id,
        package_quantity: "2",
      },
    ],
  });
  const s2 = await create({
    customer_id: alice.id,
    fulfillment_method: "DELIVERY",
    lines: [
      {
        line_type: "CUSTOM_QUANTITY",
        product_id: lemonade.id,
        underlying_quantity: "4",
        charged_unit_price: "20.00",
        packaging_cost_per_package: "0.50",
      },
    ],
  });
  const s3 = await create({
    fulfillment_method: "PICKUP",
    lines: [
      {
        line_type: "CUSTOM_ITEM",
        display_name: "Gift basket",
        underlying_quantity: "1",
        charged_unit_price: "15.00",
        custom_direct_cost_estimate: "4.00",
        manual_fulfillment_required: false,
      },
    ],
  });
  await t.api.post(`/orders/${s1.id}/payments`, {
    amount: "10.00",
    payment_method: "cash",
    payment_date: t.today(),
  });
  await t.api.post(`/orders/${s2.id}/payments`, {
    amount: "20.00",
    payment_method: "cash",
    payment_date: t.today(),
  });
  await complete(s1);
  await complete(s2);
  await complete(s3);

  const s4 = await create({
    customer_id: alice.id,
    fulfillment_method: "PICKUP",
    lines: [
      {
        line_type: "CUSTOM_QUANTITY",
        product_id: lemonade.id,
        underlying_quantity: "2",
        charged_unit_price: "9.00",
      },
    ],
  });
  const confirmed = await confirm(s4);
  await t.api.post(`/orders/${s4.id}/cancel`, { version: confirmed.version });
  t.entityIds.order = s1.id;
  return { lemonade, alice, s1, s2, s3, s4 };
}

async function openAnalytics(t: Tenant, tab = "overview", extra = "") {
  await t.page.goto(`/app/analytics?${RANGE}&tab=${tab}${extra}`);
  await expect(t.page.getByRole("heading", { name: "Analytics" })).toBeVisible();
}

async function downloadCsv(t: Tenant, linkName: string): Promise<string[][]> {
  const [download] = await Promise.all([
    t.page.waitForEvent("download"),
    t.page.getByRole("link", { name: linkName }).click(),
  ]);
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(chunk as Buffer);
  const text = Buffer.concat(chunks)
    .toString("utf-8")
    .replace(/^\uFEFF/, "");
  // These exports contain no embedded newlines or quotes in this scenario.
  return text
    .trim()
    .split("\r\n")
    .map((line) => line.split(","));
}

test("T111 Overview: KPIs, optional comparison, trend chart + table, and trend CSV agree", async ({
  t,
}) => {
  await seed(t);
  await openAnalytics(t, "overview", "&compare=0");

  const revenue = t.page.getByTestId("kpi-revenue");
  await expect(revenue).toContainText("Revenue (recorded Order revenue)");
  await expect(revenue).toContainText("$56.00");
  await expect(t.page.getByTestId("kpi-orders")).toContainText("3");
  await expect(t.page.getByTestId("kpi-aov")).toContainText("$18.67");
  const internal = t.page.getByRole("region", { name: "Internal metrics" });
  await expect(internal).toContainText("not a customer-facing amount");
  await expect(internal.getByTestId("kpi-direct-cost")).toContainText("$29.10");
  await expect(internal.getByTestId("kpi-contribution")).toContainText("$26.90");
  await expect(internal.getByTestId("kpi-margin")).toContainText("48.0%");
  await expect(t.page.getByText(/vs previous period/)).toHaveCount(0);
  await t.shot(t.page, "overview");

  // Comparison: the previous 7 days have no completed Orders -> no fabricated growth.
  await t.page.getByLabel("Compare overview KPIs to previous period").check();
  await expect(t.page.getByTestId("comparison-note")).toContainText("KPI cards only");
  await expect(revenue).toContainText("No prior-period completed-order data");
  await expect(revenue).not.toContainText("%");
  await t.shot(t.page, "overview-compare");

  // Trend: Recharts renders an SVG and the same data is a readable table.
  await expect(t.page.getByTestId("trend-chart").locator("svg[role=application]")).toBeVisible();
  const table = t.page.getByRole("table", { name: "Trend data (daily)" });
  await expect(table.getByRole("row")).toHaveCount(1 + 7 + 1);
  const todayRow = table.getByRole("row").filter({ hasText: t.today() });
  await expect(todayRow).toContainText("$56.00");
  await expect(todayRow).toContainText("$29.10");
  await expect(todayRow).toContainText("$26.90");
  await expect(todayRow).toContainText("3");

  // CSV equals the table.
  const rows = await downloadCsv(t, "Download trend CSV");
  expect(rows[0]).toEqual([
    "row_type",
    "bucket",
    "start_date",
    "end_date",
    "revenue",
    "direct_cost",
    "contribution",
    "completed_orders",
  ]);
  const bucket = rows.find((r) => r[1] === t.today());
  expect(bucket?.slice(4)).toEqual(["56.00", "29.10", "26.90", "3"]);
  const total = rows.find((r) => r[0] === "TOTAL");
  expect(total?.slice(4)).toEqual(["56.00", "29.10", "26.90", "3"]);
  expect(rows.filter((r) => r[0] === "BUCKET")).toHaveLength(7);

  // The operational Dashboard stays operational: it shows no Analytics KPI.
  await t.page.goto("/app/dashboard");
  await expect(t.page.getByText("Contribution Margin")).toHaveCount(0);
  await expect(t.page.getByText("Average Order Value")).toHaveCount(0);

  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

test("T112 Products: units vs packages, reconciliation rows, distinct best-metrics, CSV", async ({
  t,
}) => {
  await seed(t);
  await openAnalytics(t, "products");

  const table = t.page.getByRole("table", { name: /Product results/ });
  const lemonade = table.getByRole("row").filter({ hasText: "Lemonade" }).first();
  await expect(lemonade).toContainText("16"); // 12 + 4 underlying units
  await expect(lemonade).toContainText("$40.00");
  await expect(lemonade).toContainText("$25.10");
  await expect(lemonade).toContainText("$14.90");
  await expect(lemonade).toContainText("37.3%");

  await lemonade.getByRole("button", { name: /Show selling options for Lemonade/ }).click();
  await expect(t.page.getByText("2 packages")).toBeVisible();
  await expect(t.page.getByText("units only — no package count")).toBeVisible();

  await expect(t.page.getByTestId("highlight-profitable")).toContainText(
    "Most profitable — highest total Contribution",
  );
  await expect(t.page.getByTestId("highlight-margin")).toContainText(
    "Highest margin — Contribution ÷ Revenue",
  );

  // Reconciliation: products + custom items + adjustments + tax == Overview.
  await expect(t.page.getByTestId("row-custom-items")).toContainText("$15.00");
  await expect(t.page.getByTestId("row-custom-items")).toContainText("$4.00");
  await expect(t.page.getByTestId("row-tax")).toContainText("$1.00");
  await expect(t.page.getByTestId("row-adjustments")).toContainText("$0.00");
  await expect(t.page.getByTestId("row-unattributed")).toContainText("$0.00");
  await expect(t.page.getByTestId("row-totals")).toContainText("$56.00");
  await expect(t.page.getByTestId("row-totals")).toContainText("$29.10");
  await t.shot(t.page, "products");

  // Sorting is server-side and the CSV mirrors the current sort.
  await t.page.getByLabel("Sort products by").selectOption("margin");
  await expect(t.page.getByRole("link", { name: "Download products CSV" })).toHaveAttribute(
    "href",
    /sort=margin/,
  );
  const rows = await downloadCsv(t, "Download products CSV");
  const header = rows[0];
  const col = (name: string) => header.indexOf(name);
  const product = rows.find((r) => r[0] === "PRODUCT" && r[col("product_name")] === "Lemonade");
  expect(product?.[col("underlying_units")]).toBe("16.000000");
  expect(product?.[col("revenue")]).toBe("40.00");
  expect(product?.[col("direct_cost")]).toBe("25.10");
  const option = rows.find(
    (r) => r[0] === "SELLING_OPTION" && r[col("selling_option")] === "Six-pack",
  );
  expect(option?.[col("package_count")]).toBe("2.000000");
  expect(option?.[col("underlying_units")]).toBe("12.000000");
  const custom = rows.find((r) => r[0] === "CUSTOM_ITEMS");
  expect(custom?.[col("revenue")]).toBe("15.00");
  expect(rows.find((r) => r[0] === "MANUAL_TAX")?.[col("revenue")]).toBe("1.00");
  const totals = rows.find((r) => r[0] === "TOTAL");
  expect(totals?.[col("revenue")]).toBe("56.00");
  expect(totals?.[col("direct_cost")]).toBe("29.10");

  t.assertBrowserHealthy();
});

test("T113 Customers and Orders: new customer, Guest apart, canceled and outstanding balance, CSVs", async ({
  t,
}) => {
  await seed(t);

  await openAnalytics(t, "customers");
  await expect(t.page.getByTestId("kpi-unique-customers")).toContainText("1");
  await expect(t.page.getByTestId("kpi-new-customers")).toContainText("1");
  await expect(t.page.getByTestId("kpi-returning-customers")).toContainText("0");
  const guest = t.page.getByTestId("kpi-guest");
  await expect(guest).toContainText("$15.00 Revenue");
  await expect(guest).toContainText("33.3% of completed Orders");
  const customers = t.page.getByRole("table", { name: /Customers with a Completed Order/ });
  await expect(customers.getByRole("row")).toHaveCount(2);
  const alice = customers.getByRole("row").filter({ hasText: "Alice Analytics" });
  await expect(alice).toContainText("New");
  await expect(alice).toContainText("$41.00");
  await expect(alice).toContainText("$20.50");
  await expect(customers.getByText("Guest")).toHaveCount(0);
  await t.shot(t.page, "customers");
  const customerCsv = await downloadCsv(t, "Download customers CSV");
  expect(customerCsv.filter((r) => r[0] === "CUSTOMER")).toHaveLength(1);
  const guestRow = customerCsv.find((r) => r[0] === "GUEST_SUMMARY");
  expect(guestRow?.slice(4, 7)).toEqual(["1", "15.00", "15.00"]);

  await openAnalytics(t, "orders");
  const outstanding = t.page.getByTestId("kpi-outstanding");
  await expect(outstanding).toContainText("Outstanding recorded balance — as of now");
  await expect(outstanding).toContainText("$26.00"); // S1 $11.00 + S3 $15.00
  await expect(outstanding).toContainText("2 completed Orders");
  await expect(t.page.getByTestId("kpi-canceled")).toContainText("1");
  await expect(t.page.getByTestId("kpi-completed")).toContainText("3");
  await expect(t.page.getByTestId("kpi-largest")).toContainText("$21.00");
  const orders = t.page.getByRole("table", { name: /completed or canceled/ });
  await expect(orders.getByRole("row")).toHaveCount(1 + 4);
  const canceledRow = orders
    .getByRole("row")
    .filter({ has: t.page.getByText("Canceled", { exact: true }) });
  await expect(canceledRow).toHaveCount(1);
  await expect(canceledRow).toContainText("$9.00");
  await t.shot(t.page, "orders");

  // Only outstanding Orders.
  await t.page.getByLabel("Outstanding balance only").check();
  await expect(orders.getByRole("row")).toHaveCount(1 + 2);
  await t.page.getByLabel("Outstanding balance only").uncheck();

  const orderCsv = await downloadCsv(t, "Download orders CSV");
  expect(orderCsv).toHaveLength(1 + 4);
  const h = orderCsv[0];
  const completedRows = orderCsv.filter((r) => r[h.indexOf("status")] === "COMPLETED");
  expect(completedRows).toHaveLength(3);
  const sum = (name: string) =>
    completedRows.reduce((acc, r) => acc + Math.round(Number(r[h.indexOf(name)]) * 100), 0);
  expect(sum("final_total")).toBe(5600);
  expect(sum("direct_cost")).toBe(2910);
  expect(sum("contribution")).toBe(2690);
  const canceled = orderCsv.find((r) => r[h.indexOf("status")] === "CANCELED");
  expect(canceled?.[h.indexOf("direct_cost")]).toBe("");
  expect(canceled?.[h.indexOf("outstanding_balance")]).toBe("");

  t.assertBrowserHealthy();
});

test("T114 Period control, URL state, validation, empty state, keyboard tabs and mobile layout", async ({
  t,
}) => {
  t.expectResponses("ANALYTICS_RANGE_INVALID");
  await seed(t);
  await openAnalytics(t, "overview", "&compare=0");

  // Preset change updates URL and data; a past custom range is empty.
  await t.page.getByRole("button", { name: "Last 90 days" }).click();
  await expect(t.page).toHaveURL(/range=LAST_90_DAYS/);
  await expect(t.page.getByTestId("kpi-revenue")).toContainText("$56.00");
  await t.page.getByLabel("Start date").fill("2020-01-01");
  await t.page.getByLabel("End date").fill("2020-01-31");
  await t.page.getByRole("button", { name: "Apply custom range" }).click();
  await expect(t.page).toHaveURL(/range=CUSTOM/);
  await expect(t.page).toHaveURL(/start=2020-01-01/);
  await expect(t.page).toHaveURL(/end=2020-01-31/);
  await expect(t.page.getByTestId("kpi-revenue")).toContainText("$0.00");
  await expect(t.page.getByText("No Orders were completed in this period.")).toBeVisible();
  await expect(t.page.getByTestId("kpi-margin")).toContainText("—");
  await expect(t.page.getByTestId("kpi-aov")).toContainText("—");

  // Client-side guard for reversed dates.
  await t.page.getByLabel("Start date").fill("2020-02-01");
  await t.page.getByLabel("End date").fill("2020-01-01");
  await t.page.getByRole("button", { name: "Apply custom range" }).click();
  await expect(t.page.getByRole("alert")).toContainText("cannot be after");

  // The server's structured 422 is shown for a hand-edited URL.
  await t.page.goto("/app/analytics?range=CUSTOM&start=2026-02-01&end=2026-01-01&compare=0");
  await expect(t.page.getByRole("alert")).toContainText("not valid");

  // A Custom Range over 3,660 days is rejected by the server with a clear message, no Retry
  // button, and no partial trend/KPI data on screen.
  await t.page.goto("/app/analytics?range=CUSTOM&start=2010-01-01&end=2026-01-01&compare=0");
  const tooLarge = t.page.getByRole("alert");
  await expect(tooLarge).toContainText("at most 3,660 days");
  await expect(tooLarge.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(t.page.getByTestId("kpi-revenue")).toHaveCount(0);
  await expect(t.page.getByTestId("trend-chart")).toHaveCount(0);

  // Keyboard tab navigation, with a deep link that survives reload.
  await openAnalytics(t, "overview", "&compare=0");
  await t.page.getByRole("tab", { name: "Overview" }).focus();
  await t.page.keyboard.press("ArrowRight");
  await expect(t.page.getByRole("tab", { name: "Products", selected: true })).toBeVisible();
  await t.page.reload();
  await expect(t.page.getByRole("tab", { name: "Products", selected: true })).toBeVisible();

  // Desktop navigation is unchanged: at a desktop width the header is still a single row.
  await t.page.setViewportSize({ width: 1280, height: 800 });
  await openAnalytics(t, "overview", "&compare=0");
  const desktopHeader = await t.page.locator("header").first().boundingBox();
  expect(desktopHeader!.height, "desktop header stays one row").toBeLessThan(80);

  // Mobile width (375 px): the DOCUMENT must not scroll horizontally — the app shell's primary
  // navigation wraps instead of overflowing — while Analytics and Log out stay reachable, and
  // the Analytics content itself fits (tables scroll inside their own container).
  await t.page.setViewportSize({ width: 375, height: 812 });
  await openAnalytics(t, "overview", "&compare=0");
  await expect(t.page.getByTestId("kpi-revenue")).toBeVisible();
  const overflow = await t.page.evaluate(() => {
    const main = document.querySelector("main")!;
    const widest = Array.from(main.querySelectorAll<HTMLElement>("*"))
      .filter((el) => !el.closest("[data-slot=table-container]"))
      .reduce((max, el) => Math.max(max, el.getBoundingClientRect().right), 0);
    return {
      main: main.scrollWidth - main.clientWidth,
      content: widest - document.documentElement.clientWidth,
      document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  t.note(`mobile overflow px: ${JSON.stringify(overflow)}`);
  expect(overflow.document, "document-level horizontal overflow at 375px").toBeLessThanOrEqual(1);
  expect(overflow.main).toBeLessThanOrEqual(1);
  expect(overflow.content).toBeLessThanOrEqual(1);
  await expect(t.page.getByRole("link", { name: "Analytics" })).toBeInViewport();
  await expect(t.page.getByRole("button", { name: "Log out" })).toBeInViewport();
  await t.page.getByRole("link", { name: "Dashboard" }).click();
  await expect(t.page).toHaveURL(/\/app\/dashboard/);
  await t.page.getByRole("link", { name: "Analytics" }).click();
  await expect(t.page).toHaveURL(/\/app\/analytics/);
  await expect(t.page.getByRole("heading", { name: "Analytics" })).toBeVisible();
  await expect(t.page.getByTestId("kpi-revenue")).toBeVisible();
  await t.shot(t.page, "mobile-overview");

  t.assertBrowserHealthy();
});
