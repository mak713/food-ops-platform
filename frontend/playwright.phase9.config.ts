import path from "node:path";
import { defineConfig } from "@playwright/test";

// Phase 9 acceptance (test-only configuration), mirroring the Phase 8 harness: runs against
// the real stack — browser -> Vite dev server (:5173, proxying /api) -> FastAPI (:8000) ->
// PostgreSQL — so both servers must already be running. Serial, one worker: every scenario
// owns a fresh tenant, and a single worker keeps evidence ordering deterministic and avoids
// the auth rate limiter. Evidence goes to test-artifacts/phase9 (the Phase 8 evidence
// directory is never touched). The default Playwright configuration ignores this suite.
process.env.E2E_ARTIFACTS_DIR = path.resolve(import.meta.dirname, "../test-artifacts/phase9");

export default defineConfig({
  testDir: "./e2e/phase9",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  outputDir: "../test-artifacts/phase9/pw-output",
  reporter: [
    ["list"],
    ["json", { outputFile: "../test-artifacts/phase9/playwright-report.json" }],
  ],
  use: {
    baseURL: "http://localhost:5173",
    actionTimeout: 15_000,
  },
});
