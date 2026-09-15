import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RouteErrorPage } from "@/components/ui/RouteErrorPage";
import { I18nProvider } from "@/i18n/I18nProvider";
import { installAssetLoadRecovery } from "@/lib/assetLoadRecovery";

function preloadError(message: string) {
  const event = new Event("vite:preloadError", { cancelable: true });
  Object.assign(event, { payload: new TypeError(message) });
  window.dispatchEvent(event);
  return event;
}

describe("deployment asset recovery", () => {
  let cleanup: (() => void) | undefined;

  beforeEach(() => window.sessionStorage.clear());
  afterEach(() => {
    cleanup?.();
    vi.restoreAllMocks();
  });

  it.each([
    "Failed to fetch dynamically imported module: https://example.com/assets/old.js",
    "error loading dynamically imported module: https://example.com/assets/old.js",
    "Importing a module script failed.",
    "'text/html' is not a valid JavaScript MIME type.",
    "Unable to preload CSS for /assets/old.css",
  ])("reloads an asset failure once and preserves the task URL: %s", (message) => {
    window.history.replaceState(null, "", "/frontier/live?taskId=asg_test#results");
    const originalUrl = window.location.href;
    const reload = vi.fn();
    cleanup = installAssetLoadRecovery(window, reload);

    expect(preloadError(message).defaultPrevented).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe(originalUrl);

    // Simulate reinstalling the listener after a page reload in the same tab.
    cleanup();
    cleanup = installAssetLoadRecovery(window, reload);
    expect(preloadError(message).defaultPrevented).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not hide or automatically reload module evaluation errors", () => {
    const reload = vi.fn();
    cleanup = installAssetLoadRecovery(window, reload);
    expect(preloadError("Cannot read properties of undefined (reading 'score')").defaultPrevented).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it("leaves failures to the route boundary when storage cannot protect against reload loops", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage denied"); });
    const reload = vi.fn();
    cleanup = installAssetLoadRecovery(window, reload);
    expect(preloadError("Importing a module script failed.").defaultPrevented).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("route error page", () => {
  it.each([
    [new TypeError("'text/html' is not a valid JavaScript MIME type."), "页面资源未能加载"],
    [new Error("Unexpected rendering failure"), "页面暂时无法显示"],
  ])("provides a manual recovery page for %s", async (error, title) => {
    const router = createMemoryRouter([{
      path: "/tasks/:taskId/results",
      element: <div>Results</div>,
      hydrateFallbackElement: <div>Loading</div>,
      loader: () => { throw error; },
      errorElement: <RouteErrorPage />,
    }], { initialEntries: ["/tasks/asg_test/results"] });
    render(<I18nProvider><RouterProvider router={router} /></I18nProvider>);

    expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载当前页面" })).toBeInTheDocument();
    expect(screen.queryByText("Unexpected Application Error!")).not.toBeInTheDocument();
  });
});
