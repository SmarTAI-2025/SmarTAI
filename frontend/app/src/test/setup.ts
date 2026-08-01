import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Auto-cleanup the DOM between tests so each test starts from a clean tree.
afterEach(() => {
  cleanup();
});
