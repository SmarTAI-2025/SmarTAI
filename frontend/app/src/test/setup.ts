import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// Auto-cleanup the DOM between tests so each test starts from a clean tree.
afterEach(() => {
  cleanup();
});

// Browser tab coordination is exercised with actual browser contexts, not Node worker channels.
vi.stubGlobal("BroadcastChannel", undefined);
