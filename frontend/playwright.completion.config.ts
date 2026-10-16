import path from "node:path";
import { defineConfig } from "@playwright/test";

// Order Completion acceptance (test-only configuration), mirroring the Phase 8 harness: runs against
// the real stack — browser -> Vite dev server (:5173, proxying /api) -> FastAPI (:8000) ->
// PostgreSQL — so both servers must already be running. Serial, one worker: every scenario
// owns a fresh tenant, and a single worker keeps evidence ordering deterministic and avoids
// the auth rate limiter. Evidence goes to test-artifacts/completion (the Phase 8 evidence
// directory is never touched). The default Playwright configuration ignores this suite.
process.env.E2E_ARTIFACTS_DIR = path.resolve(import.meta.dirname, "../test-artifacts/completion");

export default defineConfig({
  testDir: "./e2e/completion",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: "../test-artifacts/completion/pw-output",
  reporter: [
    ["list"],
    ["json", { outputFile: "../test-artifacts/completion/playwright-report.json" }],
  ],
  use: {
    baseURL: "http://localhost:5173",
    actionTimeout: 15_000,
  },
});
