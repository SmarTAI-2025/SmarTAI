import { defineConfig, devices } from "@playwright/test";
import base from "./playwright.config";

// Run this focused contract in both Chromium and Safari's browser engine.
export default defineConfig({
  ...base,
  testMatch: "workflow-navigation.spec.ts",
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
});
