// Phase 11 Acceptance — Settings and Account completion. Browser/UI -> React -> FastAPI ->
// services -> PostgreSQL, reusing the Phase 8 harness (fresh tenant per scenario, real signup,
// real teardown). Seeding goes through the authenticated API; the behaviour under acceptance is
// driven through the real UI, and state is verified with direct SQL where the claim is about
// persisted rows.
//
//   T121 persistence of every Settings group + stale Business edit recovery
//   T122 settings really drive the Dashboard warning window and the Shopping List horizon
//   T123 historical immutability (labor rate) + timezone change semantics and cache freshness
//   T124 owner profile (name), read-only email, password change, reset-flow smoke
//   T125 controlled account deletion (+ legacy /app/account redirect), second tenant intact
//   T126 two-tenant cache isolation in ONE tab (logout->login and delete->login), delayed loads
//   T127 375 px: Settings reachable, no document-level horizontal overflow
//   T128 an old-principal Settings save still in flight across logout->login never reaches the next tenant

import type { Page } from "@playwright/test";
import { Tenant, addDays, expect, test } from "../phase8/helpers";

const PASSWORD = "correct horse battery staple";

// ----------------------------------------------------------------------------- helpers

async function openArea(page: Page, tab: string, heading = tab) {
  await page.getByRole("tab", { name: tab }).click();
  await expect(page.getByRole("heading", { name: heading, level: 2 })).toBeVisible();
}

async function expectSaved(page: Page) {
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toBeVisible();
}

async function save(page: Page) {
  await page.getByRole("button", { name: "Save changes" }).click();
  await expectSaved(page);
}

function fingerprint(t: Tenant, table: string): string {
  const [row] = t.q(
    `select count(*), coalesce(md5(string_agg(x::text, ',' order by x::text)), '') from ${table} x where x.business_id=$BIZ`,
  );
  return row.join("|");
}

const HISTORY_TABLES = [
  "production_runs",
  "production_run_ingredients",
  "production_run_order_allocations",
  "order_cost_allocations",
  "orders",
  "order_lines",
  "inventory_transactions",
];

function fingerprints(t: Tenant, tables = HISTORY_TABLES): Record<string, string> {
  return Object.fromEntries(tables.map((table) => [table, fingerprint(t, table)]));
}

async function confirmOrder(t: Tenant, order: { id: string; version: number }) {
  let r = await t.api.send("post", `/orders/${order.id}/confirm`, {
    version: order.version,
    acknowledged_warning_fingerprints: [],
  });
  if (!r.ok) {
    const fingerprintsToAck = (
      (r.body as { error?: { issues?: { details?: { fingerprint?: string } }[] } }).error?.issues ??
      []
    )
      .map((i) => i.details?.fingerprint)
      .filter(Boolean);
    r = await t.api.send("post", `/orders/${order.id}/confirm`, {
      version: order.version,
      acknowledged_warning_fingerprints: fingerprintsToAck,
    });
  }
  expect(r.ok, JSON.stringify(r.body)).toBe(true);
  return r.body as { id: string; version: number; status: string };
}

async function createConfirmedOrder(
  t: Tenant,
  productId: string,
  opts: { quantity?: number; date: string; time?: string },
) {
  const order = (await t.api.post("/orders", {
    fulfillment_date: opts.date,
    ...(opts.time ? { fulfillment_time: opts.time } : {}),
    fulfillment_method: "PICKUP",
    lines: [
      {
        line_type: "CUSTOM_QUANTITY",
        product_id: productId,
        underlying_quantity: String(opts.quantity ?? 6),
        charged_unit_price: "20.00",
      },
    ],
  })) as { id: string; version: number };
  return confirmOrder(t, order);
}

async function dashboardAlertCodes(page: Page): Promise<string[]> {
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith("/api/v1/dashboard") && r.request().method() === "GET",
    ),
    page.goto("/app/dashboard"),
  ]);
  const body = (await response.json()) as { attention: { code: string }[] };
  return body.attention.map((a) => a.code);
}

async function logIn(page: Page, email: string, password = PASSWORD) {
  await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Log in", exact: true }).click();
}

// ------------------------------------------------------------------------------- T121

test("T121 every Settings group persists across reload; a second editor gets stale-edit recovery", async ({
  t,
}) => {
  t.expectResponses("STALE_VERSION");
  const page = t.page;
  const renamed = `${t.businessName} Renamed`;

  await page.goto("/app/settings");
  await expect(page.getByRole("tab", { name: "Business Profile" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.getByLabel("Business name").fill(`  ${renamed}  `);
  await page.getByLabel("Business type").fill("Home bakery");
  await page.getByLabel("Contact phone").fill("555-0100");
  await page.getByLabel("Contact email").fill("Hello@Example.com");
  await page.getByLabel("Address").fill("1 Main St\nSuite 2");
  await save(page);
  t.businessName = renamed;
  // the header follows the saved business name
  await expect(page.getByRole("banner").getByText(renamed)).toBeVisible();

  await openArea(page, "Costing & Pricing");
  await page.getByLabel(/Default labor rate/).fill("18.5");
  await page.getByLabel(/Target contribution margin/).fill("62.5");
  await save(page);
  await expect(page.getByLabel(/Default labor rate/)).toHaveValue("18.50");

  await openArea(page, "Operations");
  await page.getByLabel(/Fulfillment warning window/).fill("45");
  await page.getByLabel(/Default Shopping List horizon/).selectOption("3");
  await save(page);

  // Persisted exactly (margin stored as the ratio, never a float).
  const [row] = t.q(
    `select default_labor_rate::text, target_contribution_margin::text,
            fulfillment_warning_minutes, shopping_horizon_days, name, business_type,
            contact_phone, contact_email, address_text from businesses where id=$BIZ`,
  );
  expect(row.slice(0, 4)).toEqual(["18.50", "0.625000", "45", "3"]);
  expect(row.slice(4, 8)).toEqual([renamed, "Home bakery", "555-0100", "Hello@Example.com"]);

  // Reload: every group re-hydrates from the server.
  await page.goto("/app/settings?area=profile");
  await expect(page.getByLabel("Business name")).toHaveValue(renamed);
  await expect(page.getByLabel("Contact email")).toHaveValue("Hello@Example.com");
  await expect(page.getByLabel("Address")).toHaveValue("1 Main St\nSuite 2");
  await openArea(page, "Costing & Pricing");
  await expect(page.getByLabel(/Default labor rate/)).toHaveValue("18.50");
  await expect(page.getByLabel(/Target contribution margin/)).toHaveValue("62.5");
  await openArea(page, "Operations");
  await expect(page.getByLabel(/Fulfillment warning window/)).toHaveValue("45");
  await expect(page.getByLabel(/Default Shopping List horizon/)).toHaveValue("3");
  await t.shot(page, "settings-persisted");

  // Stale Business edit: a second editor loaded the same version, the first saves, then the
  // second tries to save and must be offered recovery (not silently overwrite).
  const second = await t.newPage();
  await second.goto("/app/settings");
  await expect(second.getByLabel("Business name")).toHaveValue(renamed);

  await page.goto("/app/settings");
  await page.getByLabel("Business name").fill("First Saver Bakery");
  await save(page);
  t.businessName = "First Saver Bakery";

  await second.getByLabel("Business name").fill("Second Saver Bakery");
  await second.getByRole("button", { name: "Save changes" }).click();
  await expect(second.getByText(/this record changed since you opened it/i)).toBeVisible();
  expect(t.q(`select name from businesses where id=$BIZ`)[0][0]).toBe("First Saver Bakery");
  await t.shot(second, "stale-edit");

  await second.getByRole("button", { name: "Refresh" }).click();
  await expect(second.getByLabel("Business name")).toHaveValue("First Saver Bakery");
  await second.getByLabel("Business name").fill("Second Saver Bakery");
  await save(second);
  t.businessName = "Second Saver Bakery";
  expect(t.q(`select name, version from businesses where id=$BIZ`)[0][0]).toBe(
    "Second Saver Bakery",
  );

  t.assertBrowserHealthy();
});

// ------------------------------------------------------------------------------- T122

test("T122 settings really drive the Dashboard warning window and the Shopping List horizon", async ({
  t,
}) => {
  const page = t.page;
  const { product } = await t.standardProduced("Risk");

  // A CONFIRMED (not Ready) order due ~40 minutes from now, Business-local.
  const soon = new Date(Date.now() + 40 * 60_000);
  const date = new Intl.DateTimeFormat("en-CA", { timeZone: t.tz }).format(soon);
  const time =
    new Intl.DateTimeFormat("en-GB", {
      timeZone: t.tz,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(soon) + ":00";
  const order = await createConfirmedOrder(t, product.id, { date, time });
  expect(order.status).toBe("CONFIRMED");

  // default window (60 min): at risk
  expect(await dashboardAlertCodes(page)).toContain("FULFILLMENT_AT_RISK");
  await expect(page.getByText(/at risk of missing (its|their) fulfillment time/i)).toBeVisible();

  // 15-minute window: 40 minutes away is outside it
  await page.goto("/app/settings?area=operations");
  await page.getByLabel(/Fulfillment warning window/).fill("15");
  await save(page);
  expect(await dashboardAlertCodes(page)).not.toContain("FULFILLMENT_AT_RISK");
  await expect(page.getByText(/at risk of missing (its|their) fulfillment time/i)).toHaveCount(0);

  // 90-minute window: inside again
  await page.goto("/app/settings?area=operations");
  await page.getByLabel(/Fulfillment warning window/).fill("90");
  await save(page);
  expect(await dashboardAlertCodes(page)).toContain("FULFILLMENT_AT_RISK");

  // The largest storable value never breaks the Dashboard.
  await page.goto("/app/settings?area=operations");
  await page.getByLabel(/Fulfillment warning window/).fill("2147483647");
  await save(page);
  expect(await dashboardAlertCodes(page)).toContain("FULFILLMENT_AT_RISK");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // Shopping List default range follows the saved horizon.
  const horizonOf = async () => {
    const [response] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes("/api/v1/shopping-list") && r.request().method() === "GET",
      ),
      page.goto("/app/production/shopping-list"),
    ]);
    return ((await response.json()) as { range: { horizon_days: number; end_date: string } }).range;
  };
  await page.goto("/app/settings?area=operations");
  await page.getByLabel(/Default Shopping List horizon/).selectOption("3");
  await save(page);
  const three = await horizonOf();
  expect(three.horizon_days).toBe(3);
  expect(three.end_date).toBe(addDays(t.today(), 3));

  await page.goto("/app/settings?area=operations");
  await page.getByLabel(/Default Shopping List horizon/).selectOption("7");
  await save(page);
  const seven = await horizonOf();
  expect(seven.horizon_days).toBe(7);
  expect(seven.end_date).toBe(addDays(t.today(), 7));
  await t.shot(page, "shopping-list-horizon");

  t.assertBrowserHealthy();
});

// ------------------------------------------------------------------------------- T123

test("T123 labor-rate and timezone changes never rewrite history; caches never keep old-zone data", async ({
  t,
}) => {
  const page = t.page;
  const { product } = await t.standardProduced("Labor");
  const analyticsKey = (body: Record<string, unknown>) => {
    const { generated_at: _generatedAt, ...rest } = body;
    void _generatedAt;
    return JSON.stringify(rest);
  };

  // --- establish history under labor rate A = 20.00 (real Start / Finish / Complete) ---------
  await page.goto("/app/settings?area=costing");
  await page.getByLabel(/Default labor rate/).fill("20");
  await save(page);
  expect(t.q(`select default_labor_rate::text from businesses where id=$BIZ`)[0][0]).toBe("20.00");

  const dayOne = addDays(t.today(), 1);
  const dayThree = addDays(t.today(), 3);
  const first = await createConfirmedOrder(t, product.id, { date: dayOne, time: "10:00:00" });
  const runId = await t.startRunUI("Labor Box", { demandDate: dayOne });
  await t.finishRunUI(runId, "6");
  const ready = (await t.order(first.id)) as { version: number; status: string };
  expect(ready.status).toBe("READY");
  const done = (await t.api.post(`/orders/${first.id}/complete`, {
    version: ready.version,
  })) as { status: string };
  expect(done.status).toBe("COMPLETED");

  // a second requirement, planned at rate A, left unstarted
  await createConfirmedOrder(t, product.id, { date: dayThree, time: "10:00:00" });

  const beforeHistory = fingerprints(t);
  const beforeAnalytics = analyticsKey(
    await t.api.get("/analytics/overview?range=LAST_7_DAYS&compare=true"),
  );
  const [snapshotA] = t.q(
    `select labor_rate_snapshot::text, actual_labor_cost::text from production_runs where business_id=$BIZ`,
  );
  expect(snapshotA[0]).toBe("20.00");

  // --- change the rate to B = 35.00 through the UI ------------------------------------------
  await page.goto("/app/settings?area=costing");
  await expect(
    page.getByText(/Applies to future production starts and subsequent recalculations/),
  ).toBeVisible();
  await page.getByLabel(/Default labor rate/).fill("35");
  await save(page);

  expect(fingerprints(t), "labor-rate change rewrote persisted history").toEqual(beforeHistory);
  expect(
    analyticsKey(await t.api.get("/analytics/overview?range=LAST_7_DAYS&compare=true")),
    "labor-rate change moved Analytics",
  ).toBe(beforeAnalytics);
  expect(
    t.q(
      `select labor_rate_snapshot::text, actual_labor_cost::text from production_runs where business_id=$BIZ`,
    )[0],
  ).toEqual(snapshotA);

  // ... and a NEW Production Start uses the new current rate.
  await t.startRunUI("Labor Box", { demandDate: dayThree });
  const snapshots = t
    .q(
      `select labor_rate_snapshot::text from production_runs where business_id=$BIZ order by created_at`,
    )
    .map((r) => r[0]);
  expect(snapshots).toEqual(["20.00", "35.00"]);
  // the earlier run's rows are still byte-identical
  expect(
    t.q(`select labor_rate_snapshot::text, actual_labor_cost::text from production_runs
         where business_id=$BIZ and status='COMPLETED'`)[0],
  ).toEqual(snapshotA);
  await t.shot(page, "labor-rate-immutable");

  // --- timezone: warm client caches first (client-side navigation keeps the QueryClient) ----
  await page.goto("/app/dashboard");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await page.getByRole("link", { name: "Analytics" }).click();
  await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
  await expect(page.getByTestId("kpi-revenue")).toBeVisible();
  await page.getByRole("link", { name: "Orders" }).click();
  await expect(page.getByRole("heading", { name: "Orders" })).toBeVisible();
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();

  const factsBefore = fingerprints(t);
  const completedBefore = t.q(
    `select id, completed_at::text from orders where business_id=$BIZ order by id`,
  );
  const overviewBefore = (await t.api.get("/analytics/overview?range=LAST_7_DAYS")) as {
    timezone: string;
  };
  expect(overviewBefore.timezone).toBe("America/New_York");

  await page.getByLabel("Business timezone").fill("Pacific/Auckland");
  await page.getByRole("button", { name: "Save changes" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("may change how existing fulfillment dates and times");
  await expect(dialog).toContainText("Analytics");
  await t.shot(page, "timezone-confirmation");
  await dialog.getByRole("button", { name: "Change timezone" }).click();
  await expectSaved(page);

  // Persisted facts unchanged; interpretation follows the new zone.
  expect(fingerprints(t), "timezone change rewrote persisted history").toEqual(factsBefore);
  expect(
    t.q(`select id, completed_at::text from orders where business_id=$BIZ order by id`),
  ).toEqual(completedBefore);
  const overviewAfter = (await t.api.get("/analytics/overview?range=LAST_7_DAYS")) as {
    timezone: string;
  };
  expect(overviewAfter.timezone).toBe("Pacific/Auckland");

  // Previously visited, now-cached screens must not paint old-zone data: with the response
  // held back, the Dashboard and Analytics show their loading state (their cache was dropped),
  // then the fresh new-zone response.
  await page.route(/\/api\/v1\/(dashboard|analytics)/, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    await route.continue();
  });
  const dashboardResponse = page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/dashboard") && r.request().method() === "GET",
  );
  await page.getByRole("link", { name: "Dashboard" }).click();
  await expect(page.getByText("Loading…")).toBeVisible({ timeout: 1500 });
  expect(((await (await dashboardResponse).json()) as { timezone: string }).timezone).toBe(
    "Pacific/Auckland",
  );

  const analyticsResponse = page.waitForResponse(
    (r) => r.url().includes("/api/v1/analytics/overview") && r.request().method() === "GET",
  );
  await page.getByRole("link", { name: "Analytics" }).click();
  await expect(page.getByText("Loading overview…")).toBeVisible({ timeout: 1500 });
  expect(((await (await analyticsResponse).json()) as { timezone: string }).timezone).toBe(
    "Pacific/Auckland",
  );
  await page.unroute(/\/api\/v1\/(dashboard|analytics)/);

  expect(t.q(`select timezone from businesses where id=$BIZ`)[0][0]).toBe("Pacific/Auckland");
  await t.assertGlobalInvariants();
  t.assertBrowserHealthy();
});

// ------------------------------------------------------------------------------- T124

test("T124 owner profile, read-only email, password change and reset-flow smoke", async ({ t }) => {
  t.expectResponses("AUTH_INVALID_CREDENTIALS", "AUTH_UNAUTHENTICATED");
  const page = t.page;
  const NEW_PASSWORD = "a completely different passphrase";

  await page.goto("/app/settings?area=account");
  await expect(page.getByRole("heading", { name: "Account", level: 2 })).toBeVisible();
  await expect(page.getByTestId("owner-email")).toHaveText(t.email);
  await expect(page.locator(`input[value="${t.email}"]`)).toHaveCount(0); // not editable

  await page.getByLabel("Your name").fill("Renamed Owner");
  await page.getByRole("button", { name: "Save name" }).click();
  await expectSaved(page);
  await page.reload();
  await expect(page.getByLabel("Your name")).toHaveValue("Renamed Owner");
  expect(((await t.api.get("/auth/me")) as { user: { name: string } }).user.name).toBe(
    "Renamed Owner",
  );
  expect(t.q(`select name, email from users where email='${t.email}'`)[0]).toEqual([
    "Renamed Owner",
    t.email,
  ]);

  // Change password in the UI, then log out and in with it.
  await page.locator("#current_password").fill(PASSWORD);
  await page.locator("#new_password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByText("Password changed.")).toBeVisible();

  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await logIn(page, t.email, PASSWORD); // the old password no longer works
  await expect(page.getByRole("alert")).toContainText(/incorrect email or password/i);
  await page.getByLabel("Password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Log in", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/dashboard/);

  // Restore the original password so the harness teardown can authenticate the deletion.
  await page.goto("/app/settings?area=account");
  await page.locator("#current_password").fill(NEW_PASSWORD);
  await page.locator("#new_password").fill(PASSWORD);
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByText("Password changed.")).toBeVisible();

  // Reset flow smoke: reachable from the login page and renders its request form.
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.getByRole("link", { name: "Forgot your password?" }).click();
  await expect(page.getByRole("heading", { name: "Reset your password" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Send reset link" })).toBeVisible();

  await page.goto("/login");
  await logIn(page, t.email);
  await expect(page).toHaveURL(/\/app\/dashboard/);
  t.assertBrowserHealthy();
});

// ------------------------------------------------------------------------------- T125

test("T125 controlled account deletion removes exactly this tenant; legacy /app/account redirects", async ({
  t,
  browser,
}) => {
  t.expectOwnAccountDeletion();
  t.expectResponses("AUTH_INVALID_CREDENTIALS", "AUTH_UNAUTHENTICATED");
  const page = t.page;
  const other = await Tenant.create(browser, t.testInfo);
  other.slug = `${t.slug}-other`;
  try {
    const keep = await other.purchasedProduct("Survivor Lemonade", 10, 1.5);
    const otherBefore = fingerprints(other, [...HISTORY_TABLES, "products", "customers"]);
    expect(keep.id).toBeTruthy();

    // seed some of this tenant's own data so the deletion has real rows to remove
    await t.purchasedProduct("Doomed Lemonade", 10, 1.5);
    await t.api.post("/customers", { name: "Doomed Customer" });
    expect(t.q(`select count(*) from products where business_id=$BIZ`)[0][0]).toBe("1");

    // legacy URL -> the Account area of Settings
    await page.goto("/app/account");
    await expect(page).toHaveURL(/\/app\/settings\?area=account/);
    await expect(page.getByRole("heading", { name: "Account", level: 2 })).toBeVisible();

    // the exact business name is required (client check — nothing is sent)
    await page.locator("#delete_current_password").fill(PASSWORD);
    await page.locator("#business_name_confirmation").fill(t.businessName.toLowerCase());
    await page.getByRole("button", { name: "Delete account" }).click();
    await expect(page.getByText(/exactly to confirm/)).toBeVisible();
    expect(t.q(`select count(*) from businesses where id=$BIZ`)[0][0]).toBe("1");

    // right name, wrong password: refused server-side, tenant untouched
    await page.locator("#delete_current_password").fill("not the password at all");
    await page.locator("#business_name_confirmation").fill(t.businessName);
    await page.getByRole("button", { name: "Delete account" }).click();
    await expect(
      page.getByRole("alert").filter({ hasText: /password is incorrect/i }),
    ).toBeVisible();
    expect(t.q(`select count(*) from businesses where id=$BIZ`)[0][0]).toBe("1");
    await t.shot(page, "delete-refused");

    // both correct: deleted, signed out, on the login page
    await page.locator("#delete_current_password").fill(PASSWORD);
    await page.getByRole("button", { name: "Delete account" }).click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();

    // The old session is dead, and the tenant + user are truly gone (independent SQL proof).
    const me = await page.request.get("/api/v1/auth/me");
    expect(me.status()).toBe(401);
    expect(
      t.q(
        `select (select count(*) from businesses where id=$BIZ),
                (select count(*) from users where email='${t.email}'),
                (select count(*) from products where business_id=$BIZ),
                (select count(*) from customers where business_id=$BIZ),
                (select count(*) from sessions s join users u on u.id=s.user_id where u.email='${t.email}')`,
      )[0],
    ).toEqual(["0", "0", "0", "0", "0"]);
    await t.shot(page, "deleted");

    // The other tenant is completely intact and still works.
    expect(fingerprints(other, [...HISTORY_TABLES, "products", "customers"])).toEqual(otherBefore);
    await other.page.goto("/app/settings");
    await expect(other.page.getByLabel("Business name")).toHaveValue(other.businessName);
    t.assertBrowserHealthy();
  } finally {
    await other.finish();
  }
});

// ------------------------------------------------------------------------------- T126

test("T126 tenant data never crosses a principal boundary in one tab (logout->login, delete->login)", async ({
  t,
  browser,
}) => {
  t.expectResponses("AUTH_UNAUTHENTICATED");
  const page = t.page;
  const stamp = Math.random().toString(36).slice(2, 8).toUpperCase();
  const ALPHA = `ALPHA-ONLY-${stamp}`;
  const BRAVO = `BRAVO-ONLY-${stamp}`;

  const b = await Tenant.create(browser, t.testInfo);
  b.slug = `${t.slug}-B`;
  b.expectOwnAccountDeletion();
  b.expectResponses("AUTH_UNAUTHENTICATED");
  try {
    await t.api.post("/customers", { name: ALPHA });
    await b.api.post("/customers", { name: BRAVO });

    // Warm A's caches with client-side navigation only (a reload would empty the QueryClient).
    await page.goto("/app/dashboard");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await page.getByRole("link", { name: "Customers" }).click();
    await expect(page.getByText(ALPHA)).toBeVisible();
    await page.getByRole("link", { name: "Orders" }).click();
    await expect(page.getByRole("heading", { name: "Orders" })).toBeVisible();
    await page.getByRole("link", { name: "Analytics" }).click();
    await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
    await page.getByRole("link", { name: "Dashboard" }).click();

    // Continuous in-page watcher: records any appearance of an armed marker in the DOM.
    await page.evaluate(
      ([alpha, bravo]) => {
        const w = window as unknown as {
          __leaks: string[];
          __armed: Record<string, boolean>;
        };
        w.__leaks = [];
        w.__armed = { [alpha]: false, [bravo]: false };
        const check = () => {
          const text = document.body.textContent ?? "";
          for (const marker of Object.keys(w.__armed)) {
            if (w.__armed[marker] && text.includes(marker)) {
              w.__leaks.push(`${marker} @ ${location.pathname}`);
            }
          }
        };
        new MutationObserver(check).observe(document.documentElement, {
          childList: true,
          subtree: true,
          characterData: true,
        });
      },
      [ALPHA, BRAVO],
    );
    const setArmed = (marker: string, armed: boolean) =>
      page.evaluate(
        ([m, on]) => {
          (window as unknown as { __armed: Record<string, boolean> }).__armed[m as string] =
            on as boolean;
        },
        [marker, armed],
      );
    const arm = (marker: string) => setArmed(marker, true);
    const leaks = () => page.evaluate(() => (window as unknown as { __leaks: string[] }).__leaks);

    // ---- logout A -> login B, with every tenant-data response of B held back for 1.5 s ----
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
    await arm(ALPHA);

    const slow =
      /\/api\/v1\/(customers|orders|dashboard|analytics|production|shopping-list|surplus|products|ingredients|purchased)/;
    await page.route(slow, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.continue();
    });

    await logIn(page, b.email);
    await expect(page).toHaveURL(/\/app\/dashboard/);
    await expect(page.getByText("Loading…")).toBeVisible(); // nothing of A is painted meanwhile
    await expect(page.getByRole("banner").getByText(b.businessName)).toBeVisible();

    await page.getByRole("link", { name: "Customers" }).click();
    await expect(page.getByText("Loading…")).toBeVisible(); // A's cached list must be gone
    await expect(page.getByText(BRAVO)).toBeVisible();
    await expect(page.getByText(ALPHA)).toHaveCount(0);
    await page.getByRole("link", { name: "Orders" }).click();
    await expect(page.getByRole("heading", { name: "Orders" })).toBeVisible();
    await page.getByRole("link", { name: "Analytics" }).click();
    await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
    await expect(page.getByTestId("kpi-revenue")).toBeVisible();
    expect(await leaks(), "tenant A data appeared for tenant B").toEqual([]);
    await setArmed(ALPHA, false); // A legitimately returns in the second half
    await t.shot(page, "b-after-a");

    // ---- B deletes its account in this same tab, then A logs back in ---------------------
    await page.getByRole("link", { name: "Settings" }).click();
    await page.getByRole("tab", { name: "Account" }).click();
    await page.locator("#delete_current_password").fill(PASSWORD);
    await page.locator("#business_name_confirmation").fill(b.businessName);
    await page.getByRole("button", { name: "Delete account" }).click();
    await expect(page).toHaveURL(/\/login/);
    expect(
      b.q(
        `select (select count(*) from businesses where id=$BIZ),
                (select count(*) from users where email='${b.email}')`,
      )[0],
    ).toEqual(["0", "0"]);
    await arm(BRAVO);

    await logIn(page, t.email);
    await expect(page).toHaveURL(/\/app\/dashboard/);
    await expect(page.getByText("Loading…")).toBeVisible();
    await page.getByRole("link", { name: "Customers" }).click();
    await expect(page.getByText("Loading…")).toBeVisible();
    await expect(page.getByText(ALPHA)).toBeVisible();
    await expect(page.getByText(BRAVO)).toHaveCount(0);
    expect(await leaks(), "deleted tenant B data appeared for tenant A").toEqual([]);
    await page.unroute(slow);
    await t.shot(page, "a-after-delete");

    t.assertBrowserHealthy();
    b.assertBrowserHealthy();
  } finally {
    await b.finish();
  }
});

// ------------------------------------------------------------------------------- T127

test("T127 at 375 px Settings is reachable and the document does not overflow horizontally", async ({
  t,
}) => {
  const page = t.page;
  await page.setViewportSize({ width: 375, height: 812 });
  const overflow = () =>
    page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );

  await page.goto("/app/settings");
  await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  for (const [tab, label] of [
    ["Business Profile", "Business name"],
    ["Costing & Pricing", /Default labor rate/],
    ["Operations", /Fulfillment warning window/],
    ["Account", "Your name"],
  ] as const) {
    await page.getByRole("tab", { name: tab }).click();
    await expect(page.getByRole("heading", { name: tab, level: 2 })).toBeVisible();
    const field = page.getByLabel(label);
    await field.scrollIntoViewIfNeeded();
    await expect(field).toBeVisible();
    expect(await overflow(), `${tab} overflows horizontally`).toBeLessThanOrEqual(1);
  }

  // Business area: Save is reachable; the Account area's destructive control is reachable.
  await page.getByRole("tab", { name: "Business Profile" }).click();
  await page.getByLabel("Business name").fill("Narrow Bakery");
  const saveButton = page.getByRole("button", { name: "Save changes" });
  await saveButton.scrollIntoViewIfNeeded();
  await expect(saveButton).toBeVisible();
  await saveButton.click();
  await expectSaved(page);
  t.businessName = "Narrow Bakery";
  await page.getByRole("tab", { name: "Account" }).click();
  const deleteButton = page.getByRole("button", { name: "Delete account" });
  await deleteButton.scrollIntoViewIfNeeded();
  await expect(deleteButton).toBeVisible();
  expect(await overflow()).toBeLessThanOrEqual(1);
  await t.shot(page, "narrow-account");

  // the shell itself (Phase 10 protection) still fits
  await page.goto("/app/dashboard");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  expect(await overflow()).toBeLessThanOrEqual(1);
  t.assertBrowserHealthy();
});

// ------------------------------------------------------------------------------- T128

test("T128 an old-principal Settings save still in flight across logout->login never reaches the next tenant", async ({
  t,
  browser,
}) => {
  t.expectResponses("AUTH_UNAUTHENTICATED");
  const page = t.page;
  const stamp = Math.random().toString(36).slice(2, 8).toUpperCase();
  const RENAMED = `ALPHA-RENAMED-${stamp}`;

  const b = await Tenant.create(browser, t.testInfo);
  b.slug = `${t.slug}-B`;
  try {
    await page.goto("/app/settings");
    await expect(page.getByLabel("Business name")).toBeVisible();
    await page.evaluate((marker) => {
      const w = window as unknown as { __leaks: string[] };
      w.__leaks = [];
      new MutationObserver(() => {
        if ((document.body.textContent ?? "").includes(marker)) {
          w.__leaks.push(`${marker} @ ${location.pathname}`);
        }
      }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    }, RENAMED);

    // A's Settings save reaches the server (A's session is still valid), but its RESPONSE is
    // held back in the browser: the mutation stays pending in the client.
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let served = false;
    await page.route("**/api/v1/settings/business", async (route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      const response = await route.fetch();
      served = true;
      await gate;
      await route.fulfill({ response });
    });
    await page.getByLabel("Business name").fill(RENAMED);
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect.poll(() => served).toBe(true);
    expect(t.q(`select name from businesses where id=$BIZ`)[0][0]).toBe(RENAMED);

    // A logs out and B logs in while A's save is still pending.
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
    await logIn(page, b.email);

    // B is authenticated, but its shell must stay closed while A's mutation can still run
    // its callbacks (its hook-level onSuccess writes the saved settings into the cache).
    await expect(page.getByText("Loading…")).toBeVisible();
    await page.waitForTimeout(1200);
    await expect(page.getByRole("banner")).toHaveCount(0);

    release(); // A's response arrives; its callbacks run; the boundary then purges and opens
    await expect(page.getByRole("banner").getByText(b.businessName)).toBeVisible();
    await page.unroute("**/api/v1/settings/business");

    // B opens Settings with a slow load: nothing of A may be painted from cache meanwhile.
    await page.route("**/api/v1/settings", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.continue();
    });
    await page.getByRole("link", { name: "Settings" }).click();
    await expect(page.getByText("Loading settings…")).toBeVisible();
    await expect(page.getByLabel("Business name")).toHaveValue(b.businessName);
    expect(
      await page.evaluate(() => (window as unknown as { __leaks: string[] }).__leaks),
      "A's late save leaked into B's session",
    ).toEqual([]);
    await page.unroute("**/api/v1/settings");
    await t.shot(page, "b-after-held-a-save");

    // Hand the browser context back to A so the harness teardown can authenticate A's deletion.
    await page.getByRole("button", { name: "Log out" }).click();
    await logIn(page, t.email);
    await expect(page).toHaveURL(/\/app\/dashboard/);

    t.businessName = RENAMED; // A's save did apply server-side; the harness teardown needs it
    t.assertBrowserHealthy();
    b.assertBrowserHealthy();
  } finally {
    await b.finish();
  }
});
