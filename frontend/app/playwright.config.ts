import { defineConfig, devices } from "@playwright/test";

const frontendUrl = process.env.SMARTAI_E2E_FRONTEND_URL ?? "http://127.0.0.1:5173";
const frontendPort = new URL(frontendUrl).port || "5173";

/**
 * Exercise the public production bundle with a disposable backend. Vite's dev
 * server resolves /admin to the source admin.html even in public mode, so it
 * cannot verify that the deployed public build excludes the private console.
 * CI uses a fake provider for the normalized grading/review/release loop.
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
    command: `npm run build -- --mode e2e && npm run preview -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
    env: { VITE_SMARTAI_BACKEND_URL: process.env.SMARTAI_E2E_BACKEND_URL ?? "http://127.0.0.1:8000" },
    url: frontendUrl,
    reuseExistingServer: false,
    timeout: 60_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
