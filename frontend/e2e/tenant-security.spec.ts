import { expect, test } from "@playwright/test";

// Spec §19.18's "tenant-security workflow": create two tenants and attempt direct
// URL/API access to the other tenant's resources (Phase 2 plan §17/§18).
//
// Requires the backend dev server running at :8000 in addition to the frontend
// (`npm run dev`, started automatically by playwright.config.ts's webServer) — same
// two-server requirement as local manual testing. Not yet wired into CI, consistent
// with Phase 0's e2e/smoke.spec.ts note that Playwright-in-CI is deferred.

function uniqueEmail(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}@example.com`;
}

test("two tenants cannot see or reach each other's session/business data", async ({ browser }) => {
  const contextA = await browser.newContext();
  const contextB = await browser.newContext();
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();

  await pageA.goto("/signup");
  await pageA.getByLabel("Your name").fill("Tenant A Owner");
  await pageA.getByLabel("Email").fill(uniqueEmail("tenant-a"));
  await pageA.getByLabel("Password").fill("correct horse battery staple A");
  await pageA.getByLabel("Business name").fill("Tenant A Bakery");
  await pageA.getByRole("button", { name: /create account/i }).click();
  // The business name appears twice on the dashboard (header + summary line) —
  // .first() just needs any visible occurrence, not a specific one.
  await expect(pageA.getByText(/Tenant A Bakery/).first()).toBeVisible();

  await pageB.goto("/signup");
  await pageB.getByLabel("Your name").fill("Tenant B Owner");
  await pageB.getByLabel("Email").fill(uniqueEmail("tenant-b"));
  await pageB.getByLabel("Password").fill("correct horse battery staple B");
  await pageB.getByLabel("Business name").fill("Tenant B Bakery");
  await pageB.getByRole("button", { name: /create account/i }).click();
  await expect(pageB.getByText(/Tenant B Bakery/).first()).toBeVisible();

  // Neither tenant's dashboard ever mentions the other tenant's business.
  await expect(pageA.getByText(/Tenant B Bakery/)).toHaveCount(0);
  await expect(pageB.getByText(/Tenant A Bakery/)).toHaveCount(0);

  // Direct API access attempting to smuggle a foreign business id via a header still
  // resolves purely from A's own session — never honors the header.
  const response = await pageA.request.get("/api/v1/auth/me", {
    headers: { "X-Business-Id": "00000000-0000-0000-0000-000000000000" },
  });
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.business.name).toBe("Tenant A Bakery");

  await contextA.close();
  await contextB.close();
});
