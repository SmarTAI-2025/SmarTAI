import { defineConfig, devices } from "@playwright/test";

const frontendUrl = process.env.SMARTAI_E2E_FRONTEND_URL ?? "http://127.0.0.1:5173";

/**
 * Playwright config for the normalized learning-workflow E2E suite.
 *
 * The backend is started by CI (see .github/workflows/ci.yml) on
 * http://127.0.0.1:8000; the dev server (vite) runs on 5173. Tests hit the
 * backend through the app UI. The suite covers the non-LLM closed loop
 * (login → course → enroll → assignment → publish → student submit →
 * unreleased-results notice) so it runs without provider API keys; the grading
 * step needs a provider and is covered by backend unit/integration tests.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: frontendUrl,
    trace: "on-first-retry",
    ignoreHTTPSErrors: true,
  },
  webServer: {
    command: "npm run dev",
    url: frontendUrl,
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
