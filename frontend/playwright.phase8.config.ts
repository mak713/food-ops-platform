import { defineConfig } from "@playwright/test";

// Phase 8 automated manual-acceptance pass (test-only configuration). Runs against the
// real stack: browser -> Vite dev server (:5173, proxying /api) -> FastAPI (:8000) ->
// PostgreSQL. Both servers must already be running (same requirement as manual
// testing). Serial, one worker: every scenario owns a fresh tenant, but a single worker
// keeps evidence ordering deterministic and avoids the auth rate limiter.
export default defineConfig({
  testDir: "./e2e/phase8",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: "../test-artifacts/phase8/pw-output",
  reporter: [
    ["list"],
    ["json", { outputFile: "../test-artifacts/phase8/playwright-report.json" }],
  ],
  use: {
    baseURL: "http://localhost:5173",
    actionTimeout: 15_000,
  },
});
