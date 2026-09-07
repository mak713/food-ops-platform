import { defineConfig } from "@playwright/test";

// Playwright is installed/configured in Phase 0 but not yet wired into CI
// (Spec §18.8 lists E2E-in-CI as something to add "eventually"; deferred
// until a later phase has meaningful E2E behavior to test).
export default defineConfig({
  testDir: "./e2e",
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
