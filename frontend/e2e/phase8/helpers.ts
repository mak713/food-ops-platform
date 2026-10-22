// Phase 8 automated acceptance — shared fixtures/helpers (TEST-ONLY code).
//
// Each scenario owns a brand-new tenant (User + Business) created through the real
// signup endpoint, drives the behaviour under acceptance through the real UI, and is
// torn down through the real `DELETE /account` endpoint (Business first, then User) —
// so no tenant-owned row or owner User is left behind. Prerequisite master data
// (ingredients/products/recipes/opening balances) is seeded through the authenticated
// API; the actions being accepted are performed in the browser.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
  type TestInfo,
  expect,
  test as base,
} from "@playwright/test";

export const BASE = "http://localhost:5173";
// Outside the frontend/ tree on purpose: Vite watches frontend/ and full-reloads the page
// whenever Playwright writes trace .html resources there.
// A later phase's acceptance suite reuses this harness and points the evidence elsewhere
// via E2E_ARTIFACTS_DIR (set by its own Playwright config); unset, behavior is unchanged.
export const ARTIFACTS = process.env.E2E_ARTIFACTS_DIR
  ? path.resolve(process.env.E2E_ARTIFACTS_DIR)
  : path.resolve(import.meta.dirname, "../../../test-artifacts/phase8");
const PASSWORD = "correct horse battery staple";
const TZ = "America/New_York";

export interface EvidenceRecord {
  test: string;
  file: string;
  status: string;
  durationMs: number;
  notes: string[];
  screenshots: string[];
  entityIds: Record<string, string>;
  unexpectedResponses: { url: string; status: number; code?: string; body?: string }[];
  consoleErrors: string[];
  fatalErrors: string[];
  finalUrl: string;
  visibleAlerts: string[];
  trace?: string;
  failureScreenshot?: string;
  cleanup: string;
}

export type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

/** Direct SQL read against the dev database (test-only verification helper). */
export function sql(query: string): string[][] {
  const out = execFileSync(
    "docker",
    ["exec", "food-ops-platform-db-1", "psql", "-U", "foodops", "-d", "foodops_dev", "-At", "-F", "|", "-c", query],
    { encoding: "utf8" },
  );
  return out
    .split("\n")
    .filter((l) => l.length > 0)
    .map((l) => l.split("|"));
}

export function addDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function businessToday(tz: string = TZ): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date());
}

export class Api {
  constructor(
    private readonly req: APIRequestContext,
    private readonly ctx: BrowserContext,
  ) {}

  private async headers(): Promise<Record<string, string>> {
    const csrf = (await this.ctx.cookies()).find((c) => c.name === "fo_csrf");
    return { Origin: BASE, "X-CSRF-Token": csrf?.value ?? "" };
  }

  async get<T = Json>(p: string): Promise<T> {
    const r = await this.req.get(`${BASE}/api/v1${p}`);
    if (!r.ok()) throw new Error(`GET ${p} -> ${r.status()} ${await r.text()}`);
    return (await r.json()) as T;
  }

  async send<T = Json>(method: "post" | "put" | "patch" | "delete", p: string, data?: unknown) {
    const r = await this.req[method](`${BASE}/api/v1${p}`, { data, headers: await this.headers() });
    const text = await r.text();
    let body: unknown = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = text;
    }
    return { status: r.status(), ok: r.ok(), body: body as T, headers: r.headers() };
  }

  async post<T = Json>(p: string, data?: unknown): Promise<T> {
    const r = await this.send<T>("post", p, data);
    if (!r.ok) throw new Error(`POST ${p} -> ${r.status} ${JSON.stringify(r.body)}`);
    return r.body;
  }
}

export class Tenant {
  readonly notes: string[] = [];
  readonly screenshots: string[] = [];
  readonly entityIds: Record<string, string> = {};
  readonly unexpectedResponses: EvidenceRecord["unexpectedResponses"] = [];
  readonly consoleErrors: string[] = [];
  readonly fatalErrors: string[] = [];
  private expectedCodes = new Set<string>();
  page!: Page;
  api!: Api;
  businessId = "";
  businessName = "";
  email = "";
  tz = TZ;
  slug = "";
  private testNumber = "";
  private ownAccountDeletion = false;

  constructor(
    readonly context: BrowserContext,
    readonly testInfo: TestInfo,
  ) {}

  static async create(
    browser: Browser,
    testInfo: TestInfo,
    options: { timezoneId?: string } = {},
  ): Promise<Tenant> {
    fs.mkdirSync(path.join(ARTIFACTS, "screenshots"), { recursive: true });
    fs.mkdirSync(path.join(ARTIFACTS, "traces"), { recursive: true });
    fs.mkdirSync(path.join(ARTIFACTS, "results"), { recursive: true });
    const context = await browser.newContext({ baseURL: BASE, ...options });
    await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
    const t = new Tenant(context, testInfo);
    t.testNumber = (testInfo.title.match(/^T(\d+[A-Z]?)/)?.[1] ?? "x").padStart(2, "0");
    t.slug = testInfo.title.replace(/[^A-Za-z0-9]+/g, "-").slice(0, 60);
    t.api = new Api(context.request, context);
    t.page = await t.newPage();
    await t.signup();
    return t;
  }

  /** A new page in this tenant's (already-authenticated) browser context. */
  async newPage(): Promise<Page> {
    const page = await this.context.newPage();
    this.wire(page);
    return page;
  }

  private wire(page: Page) {
    page.on("pageerror", (e) => this.fatalErrors.push(`pageerror: ${e.message}`));
    page.on("console", (m) => {
      if (m.type() === "error") this.consoleErrors.push(m.text());
    });
    page.on("response", async (r) => {
      const url = r.url();
      if (!url.includes("/api/")) return;
      const status = r.status();
      if (status < 400) return;
      let body = "";
      let code: string | undefined;
      try {
        body = await r.text();
        code = JSON.parse(body)?.error?.code;
      } catch {
        /* non-JSON body */
      }
      if (status >= 500 || !code || !this.expectedCodes.has(code)) {
        this.unexpectedResponses.push({ url, status, code, body: body.slice(0, 500) });
      }
    });
  }

  /** Declare structured 4xx error codes this scenario intentionally provokes. */
  expectResponses(...codes: string[]) {
    codes.forEach((c) => this.expectedCodes.add(c));
  }

  /**
   * Opt-in for a scenario whose subject IS account deletion (Phase 11): this scenario deletes
   * its own tenant through the UI, so `finish()` skips its normal API deletion and instead
   * requires — via direct SQL — that the User and Business rows are genuinely gone (the test
   * fails, and the leftovers are removed, otherwise). Default scenarios are unaffected: their
   * cleanup, leak detection and `tenant cleanup` assertion are unchanged.
   */
  expectOwnAccountDeletion() {
    this.ownAccountDeletion = true;
  }

  note(msg: string) {
    this.notes.push(msg);
  }

  async shot(page: Page, name: string) {
    const file = `T${this.testNumber}-${name}.png`;
    await page.screenshot({ path: path.join(ARTIFACTS, "screenshots", file), fullPage: true });
    this.screenshots.push(file);
  }

  private async signup() {
    this.email = `p8acc-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
    this.businessName = `P8 Bakery ${Math.random().toString(36).slice(2, 8)}`;
    // The signup endpoint is rate limited (10/minute/IP — production behaviour, not
    // altered here). Wait on the server's own Retry-After rather than guessing.
    for (let attempt = 0; attempt < 8; attempt++) {
      const r = await this.api.send("post", "/auth/signup", {
        name: "Acceptance Owner",
        email: this.email,
        password: PASSWORD,
        business_name: this.businessName,
        business_timezone: this.tz,
      });
      if (r.status === 429) {
        const wait = Number(r.headers["retry-after"] ?? "10");
        await new Promise((res) => setTimeout(res, (Number.isFinite(wait) ? wait : 10) * 1000 + 250));
        continue;
      }
      if (!r.ok) throw new Error(`signup failed ${r.status} ${JSON.stringify(r.body)}`);
      break;
    }
    const me = await this.api.get("/auth/me");
    this.businessId = me.business.id;
    this.entityIds.business = this.businessId;
    // Establish the session in the page's origin.
    await this.page.goto("/app/dashboard");
    await expect(this.page.getByText(this.businessName).first()).toBeVisible();
  }

  today(): string {
    return businessToday(this.tz);
  }

  // ------------------------------------------------------------------ seeding (API)

  async ingredient(name: string, quantity: number | null, unitCost: number, unit = "g") {
    const created = await this.api.post("/ingredients", {
      name,
      measurement_family: "WEIGHT",
      canonical_unit: unit,
    });
    let version = created.version;
    if (quantity !== null) {
      const r = await this.api.post(`/ingredients/${created.id}/inventory/initial-balance`, {
        version,
        quantity: String(quantity),
        unit,
        unit_cost: String(unitCost),
      });
      version = r.version;
    }
    return { id: created.id as string, name, version: version as number };
  }

  async producedProduct(
    name: string,
    ingredients: { id: string; qty: number }[],
    opts: { yieldQty?: number; reusable?: boolean; usableDays?: number } = {},
  ) {
    const product = await this.api.post("/products", {
      name,
      product_type: "PRODUCED",
      can_reuse_surplus: opts.reusable ?? true,
      default_surplus_usable_days: opts.usableDays ?? null,
    });
    await this.api.post(`/products/${product.id}/recipe`, {
      name: `${name} recipe`,
      yield_quantity: String(opts.yieldQty ?? 6),
      active_time_minutes: 30,
      ingredients: ingredients.map((i) => ({ ingredient_id: i.id, quantity: String(i.qty), unit: "g" })),
    });
    return { id: product.id as string, name };
  }

  async purchasedProduct(name: string, quantity: number | null, unitCost = 1) {
    const product = await this.api.post("/products", { name, product_type: "PURCHASED" });
    if (quantity !== null) {
      await this.api.post(`/products/${product.id}/purchased-inventory/initial-balance`, {
        quantity: String(quantity),
        unit_cost: String(unitCost),
      });
    }
    return { id: product.id as string, name };
  }

  /** The standard fixture: 2 ingredients, a produced product (yield 6/batch; 120 g A + 60 g B per batch). */
  async standardProduced(prefix = "Cookie", stock = { a: 10000, b: 5000 }, opts = {}) {
    const a = await this.ingredient(`${prefix} Flour`, stock.a, 0.01);
    const b = await this.ingredient(`${prefix} Sugar`, stock.b, 0.02);
    const product = await this.producedProduct(
      `${prefix} Box`,
      [
        { id: a.id, qty: 120 },
        { id: b.id, qty: 60 },
      ],
      opts,
    );
    this.entityIds.product = product.id;
    this.entityIds.ingredientA = a.id;
    this.entityIds.ingredientB = b.id;
    return { product, a, b };
  }

  // ------------------------------------------------------------------ reads (API/SQL)

  ingredientQty = async (id: string) => (await this.api.get(`/ingredients/${id}`)).physical_quantity as string;
  ingredientTxns = async (id: string) =>
    (await this.api.get(`/ingredients/${id}/inventory/transactions`)).items as Json[];
  order = (id: string) => this.api.get(`/orders/${id}`);
  run = (id: string) => this.api.get(`/production-runs/${id}`);
  requirements = async () => (await this.api.get("/production-runs/requirements?limit=200")).items as Json[];
  surplusLots = async () => (await this.api.get("/surplus?limit=200")).items as Json[];

  q(query: string): string[][] {
    return sql(query.replaceAll("$BIZ", `'${this.businessId}'`));
  }

  /** Global invariants that must hold at any quiescent point of a scenario. */
  async assertGlobalInvariants() {
    const over = this.q(
      `select s.id from surplus_inventory s left join surplus_allocations a on a.surplus_inventory_id=s.id
       where s.business_id=$BIZ group by s.id, s.physical_quantity
       having coalesce(sum(a.quantity),0) > s.physical_quantity`,
    );
    expect(over, "SUM(active SurplusAllocation) <= physical_quantity").toEqual([]);
    const dup = this.q(
      `select surplus_inventory_id, order_line_id from surplus_allocations where business_id=$BIZ
       group by 1,2 having count(*)>1`,
    );
    expect(dup, "no duplicate (lot, line) claims").toEqual([]);
    // READY orders must genuinely be covered; fully-covered produced-only orders must not stay CONFIRMED.
    const rows = this.q(
      `select o.id, o.status,
         (select count(*) from order_lines l where l.order_id=o.id) as lines
       from orders o where o.business_id=$BIZ and o.status in ('CONFIRMED','READY')`,
    );
    for (const [id, status] of rows) {
      const lineRows = this.q(
        `select l.id, l.line_type, l.underlying_quantity, l.manual_fulfillment_required,
                l.manual_fulfillment_satisfied, p.product_type,
           coalesce((select sum(quantity) from surplus_allocations a where a.order_line_id=l.id),0),
           coalesce((select sum(quantity) from production_run_order_allocations r where r.order_line_id=l.id),0)
         from order_lines l left join products p on p.id=l.product_id where l.order_id='${id}'`,
      );
      let satisfied = lineRows.length > 0;
      for (const [, lt, qty, req, sat, ptype, surplus, prod] of lineRows) {
        if (lt === "CUSTOM_ITEM") {
          if (req === "t" && sat !== "t") satisfied = false;
        } else if (ptype === "PRODUCED") {
          if (Number(surplus) + Number(prod) < Number(qty)) satisfied = false;
        }
        // Purchased lines are checked by the scenarios that use them (aggregate shortage semantics).
      }
      const hasPurchased = lineRows.some((r) => r[5] === "PURCHASED");
      if (status === "READY") expect(satisfied, `READY order ${id} must be fully covered`).toBe(true);
      if (status === "CONFIRMED" && !hasPurchased)
        expect(satisfied, `lost readiness update for order ${id}`).toBe(false);
    }
  }

  // ------------------------------------------------------------------ UI actions

  async selectOption(trigger: ReturnType<Page["locator"]>, optionName: string | RegExp, page = this.page) {
    await trigger.click();
    await page.getByRole("option", { name: optionName }).first().click();
  }

  /**
   * Create a DRAFT order through the real Order-entry form and return its id.
   * `fulfillmentOffsetDays` is relative to the Business-local calendar date.
   */
  async createOrderUI(
    lines: (
      | { kind: "quantity"; product: string; qty: number | string; price?: string }
      | { kind: "custom-item"; description: string; qty?: number; price?: string }
    )[],
    opts: { offsetDays?: number; time?: string; page?: Page } = {},
  ): Promise<string> {
    const page = opts.page ?? this.page;
    await page.goto("/app/orders/new");
    // Let the form's reference queries (customers/products) settle before typing, so a
    // late hydration can never reset what the seller has entered.
    await page.waitForLoadState("networkidle");
    const date = addDays(this.today(), opts.offsetDays ?? 3);
    await page.locator("#fulfillment_date").fill(date);
    await page.locator("#fulfillment_time").fill(opts.time ?? "10:00");
    let i = 0;
    for (const line of lines) {
      if (line.kind === "quantity") {
        await page.getByRole("button", { name: "Add Custom Quantity line" }).click();
        await this.selectOption(page.getByLabel(`Line ${i + 1} product`), line.product, page);
        await page.locator(`[id="lines.${i}.underlying_quantity"]`).fill(String(line.qty));
        await page.locator(`[id="lines.${i}.charged_unit_price"]`).fill(line.price ?? "20.00");
      } else {
        await page.getByRole("button", { name: "Add Custom Item line" }).click();
        await page.locator(`[id="lines.${i}.display_name"]`).fill(line.description);
        await page.locator(`[id="lines.${i}.underlying_quantity"]`).fill(String(line.qty ?? 1));
        await page.locator(`[id="lines.${i}.charged_unit_price"]`).fill(line.price ?? "15.00");
      }
      i++;
    }
    await expect(page.locator("#fulfillment_date")).toHaveValue(date);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await page.waitForURL(/\/app\/orders\/[0-9a-f-]{36}$/);
    return page.url().split("/").pop() as string;
  }

  /** Confirm a Draft through the UI, acknowledging the warning review when it appears. */
  async confirmOrderUI(orderId: string, page = this.page): Promise<{ acknowledged: boolean; warnings: string[] }> {
    await page.goto(`/app/orders/${orderId}`);
    await page.getByRole("button", { name: "Confirm Order" }).click();
    const anyway = page.getByRole("button", { name: "Confirm anyway" });
    const confirmed = page.getByRole("heading", { name: /^(Confirmed|Ready)$/ });
    await expect(anyway.or(confirmed)).toBeVisible();
    let acknowledged = false;
    let warnings: string[] = [];
    if (await anyway.isVisible()) {
      warnings = await page.getByRole("alert").allInnerTexts();
      await anyway.click();
      acknowledged = true;
      await expect(confirmed).toBeVisible();
    }
    return { acknowledged, warnings };
  }

  async orderStatusBadge(orderId: string, page = this.page): Promise<string> {
    await page.goto(`/app/orders/${orderId}`);
    const badge = page.locator("span,div").filter({ hasText: /^(DRAFT|CONFIRMED|READY|COMPLETED|CANCELED)$/ }).first();
    await expect(badge).toBeVisible();
    return (await badge.innerText()).trim();
  }

  async recordSurplusUI(
    productName: string,
    quantity: string,
    opts: { unitCost?: string; usableThrough?: string; reusable?: boolean } = {},
  ) {
    const page = this.page;
    await page.goto("/app/surplus/new");
    await this.selectOption(page.locator("#product_id"), productName);
    await page.locator("#quantity").fill(quantity);
    await page.locator("#unit_cost_basis").fill(opts.unitCost ?? "1.50");
    if (opts.usableThrough) await page.locator("#usable_through_date").fill(opts.usableThrough);
    if (opts.reusable ?? true) await page.locator("#is_reusable").check();
    await page.getByRole("button", { name: "Record Surplus", exact: true }).click();
    await page.waitForURL(/\/app\/surplus$/);
  }

  /** Production Plan row -> Review & Start; returns the requirement id from the URL. */
  async openReviewAndStart(productName: string, demandDate?: string): Promise<string> {
    const page = this.page;
    await page.goto("/app/production");
    let row = page.getByRole("row").filter({ hasText: productName });
    if (demandDate) row = row.filter({ hasText: demandDate });
    await row.first().getByRole("link", { name: "Review & Start" }).click();
    await page.waitForURL(/\/app\/production\/start\/[0-9a-f-]{36}$/);
    return page.url().split("/").pop() as string;
  }

  async startRunUI(productName: string, opts: { demandDate?: string; batches?: number } = {}): Promise<string> {
    const page = this.page;
    await this.openReviewAndStart(productName, opts.demandDate);
    if (opts.batches !== undefined) await page.locator("#batches").fill(String(opts.batches));
    await page.getByRole("button", { name: "Start Production" }).click();
    await page.waitForURL(/\/app\/production\/runs\/[0-9a-f-]{36}$/);
    return page.url().split("/").pop() as string;
  }

  async finishRunUI(
    runId: string,
    output: string,
    overrides: Record<string, string> = {},
  ): Promise<void> {
    const page = this.page;
    await page.goto(`/app/production/runs/${runId}`);
    for (const [ingredientName, value] of Object.entries(overrides)) {
      await page.getByRole("row").filter({ hasText: ingredientName }).getByRole("textbox").fill(value);
    }
    await page.locator("#actual_output").fill(output);
    await page.getByRole("button", { name: "Finish Production" }).click();
    await expect(page.getByRole("heading", { name: "Finish results" })).toBeVisible();
  }

  async cancelRunUI(runId: string) {
    const page = this.page;
    await page.goto(`/app/production/runs/${runId}`);
    await page.getByRole("button", { name: "Cancel Run" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Cancel run" }).click();
    await page.waitForURL(/\/app\/production\/runs$/);
  }

  async adjustSurplusUI(lotId: string, remove: string): Promise<void> {
    const page = this.page;
    await page.goto(`/app/surplus/${lotId}/adjust`);
    await page.locator("#quantity_removed").fill(remove);
    await this.selectOption(page.locator("#reason"), "Spoilage or waste");
    await page.getByRole("button", { name: "Save adjustment" }).click();
  }

  // ------------------------------------------------------------------ teardown/evidence

  async finish() {
    const info = this.testInfo;
    const failed = info.status !== info.expectedStatus;
    let cleanup = "ok";
    let trace: string | undefined;
    let failureScreenshot: string | undefined;
    let finalUrl = "";
    let visibleAlerts: string[] = [];
    try {
      finalUrl = this.page.url();
      visibleAlerts = await this.page.getByRole("alert").allInnerTexts();
    } catch {
      /* page closed */
    }
    if (failed) {
      try {
        failureScreenshot = `FAIL-${this.slug}.png`;
        await this.page.screenshot({ path: path.join(ARTIFACTS, "screenshots", failureScreenshot), fullPage: true });
      } catch {
        failureScreenshot = undefined;
      }
    }
    if (failed) {
      trace = `traces/${this.slug}.zip`;
      await this.context.tracing.stop({ path: path.join(ARTIFACTS, trace) });
    } else {
      await this.context.tracing.stop();
    }
    // Real account deletion: Business first, then User (session + CSRF still valid).
    if (!this.ownAccountDeletion) {
      try {
        const r = await this.api.send("delete", "/account", {
          current_password: PASSWORD,
          business_name_confirmation: this.businessName,
        });
        if (!r.ok) cleanup = `account delete failed ${r.status} ${JSON.stringify(r.body)}`;
      } catch (e) {
        cleanup = `account delete error ${(e as Error).message}`;
      }
    }
    const leftover = sql(
      `select (select count(*) from users where email='${this.email}'),
              (select count(*) from businesses where id='${this.businessId}')`,
    )[0];
    if (leftover[0] !== "0" || leftover[1] !== "0") {
      if (this.ownAccountDeletion) {
        cleanup = `scenario-owned account deletion left rows (users=${leftover[0]}, businesses=${leftover[1]})`;
      }
      // Fall back to direct removal of ONLY this scenario's own tenant.
      try {
        sql(`delete from businesses where id='${this.businessId}'`);
        sql(`delete from users where email='${this.email}'`);
        cleanup += " | fallback SQL cleanup used";
      } catch (e) {
        cleanup += ` | fallback failed: ${(e as Error).message}`;
      }
      const again = sql(`select (select count(*) from users where email='${this.email}')`)[0];
      if (again[0] !== "0") cleanup = "LEFTOVER USER ROW";
    }
    const record: EvidenceRecord = {
      test: info.title,
      file: info.file,
      status: info.status ?? "unknown",
      durationMs: info.duration,
      notes: this.notes,
      screenshots: this.screenshots,
      entityIds: this.entityIds,
      unexpectedResponses: this.unexpectedResponses,
      consoleErrors: this.consoleErrors,
      fatalErrors: this.fatalErrors,
      finalUrl,
      visibleAlerts,
      trace,
      failureScreenshot,
      cleanup,
    };
    fs.writeFileSync(path.join(ARTIFACTS, "results", `${this.slug}.json`), JSON.stringify(record, null, 2));
    await this.context.close();
    expect(cleanup, "tenant cleanup").toBe("ok");
  }

  /** Browser-health assertions required across the suite: no 5xx, no unexpected 4xx, no JS errors. */
  assertBrowserHealthy() {
    expect(this.fatalErrors, "uncaught page errors").toEqual([]);
    expect(this.unexpectedResponses, "unexpected HTTP error responses").toEqual([]);
    const nonResource = this.consoleErrors.filter((m) => !/Failed to load resource: the server responded with a status of 4\d\d/.test(m));
    expect(nonResource, "browser console errors").toEqual([]);
  }
}

export const test = base.extend<{ t: Tenant; tenantOptions: { timezoneId?: string } }>({
  tenantOptions: [{}, { option: true }],
  t: async ({ browser, tenantOptions }, runTest, testInfo) => {
    const tenant = await Tenant.create(browser, testInfo, tenantOptions);
    await runTest(tenant);
    await tenant.finish();
  },
});
export { expect };
