import { defineConfig } from "@playwright/test";

// Playwright is installed/configured in Phase 0 but not yet wired into CI
// (Spec §18.8 lists E2E-in-CI as something to add "eventually"; deferred
// until a later phase has meaningful E2E behavior to test).
export default defineConfig({
  testDir: "./e2e",
  // The Phase 8 acceptance suite is a local regression harness (Docker PostgreSQL,
  // dev stack, test-tenant lifecycle); run it explicitly via playwright.phase8.config.ts
  // (Phase 9, Order Completion and Phase 10 Analytics likewise:
  // playwright.phase9.config.ts, playwright.completion.config.ts, playwright.phase10.config.ts).
  testIgnore: ["**/phase8/**", "**/phase9/**", "**/phase10/**", "**/completion/**"],
  fullyParallel: true,
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
  },
  use: {
    baseURL: "http://localhost:5173",
  },
});
