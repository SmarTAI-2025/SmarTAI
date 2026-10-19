import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createFrontierDemoSession } from "@/api/auth";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { FrontierDemoEntryPage } from "./FrontierDemoEntryPage";

vi.mock("@/api/auth", () => ({ createFrontierDemoSession: vi.fn() }));

describe("FrontierDemoEntryPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("smartai_locale", "en-US");
    vi.mocked(createFrontierDemoSession).mockReset();
  });

  it("creates a passwordless backend session and enters the live route", async () => {
    vi.mocked(createFrontierDemoSession).mockResolvedValue({
      token: "server-signed-token",
      user: { id: "frontier-1", username: "frontier-1", email: "", role: "teacher", is_active: true, created_at: 1 },
    });
    const router = createMemoryRouter([
      { path: "/frontier/enter", element: <FrontierDemoEntryPage /> },
      { path: "/frontier/live", element: <div>Live route reached</div> },
    ], { initialEntries: ["/frontier/enter"] });

    render(<QueryClientProvider client={new QueryClient()}><I18nProvider><RouterProvider router={router} /></I18nProvider></QueryClientProvider>);

    expect(screen.getByRole("heading", { name: "Starting the demo" })).toBeInTheDocument();
    expect(await screen.findByText("Live route reached")).toBeInTheDocument();
    expect(createFrontierDemoSession).toHaveBeenCalledTimes(1);
  });

  it("translates an internal disabled code into a useful Chinese message", async () => {
    window.localStorage.setItem("smartai_locale", "zh-CN");
    vi.mocked(createFrontierDemoSession).mockRejectedValue(new APIError(
      503,
      "frontier_demo_disabled",
      { detail: { code: "frontier_demo_disabled" } },
    ));
    const router = createMemoryRouter([
      { path: "/frontier/enter", element: <FrontierDemoEntryPage /> },
    ], { initialEntries: ["/frontier/enter"] });

    render(<QueryClientProvider client={new QueryClient()}><I18nProvider><RouterProvider router={router} /></I18nProvider></QueryClientProvider>);

    expect(await screen.findByText(/演示暂不可用/)).toBeInTheDocument();
    expect(screen.getByText("产品演示")).toBeInTheDocument();
    expect(document.querySelector('[data-smartai-app-mark="silver"]')).toBeInTheDocument();
    expect(document.querySelector('[data-smartai-wordmark="white"]')).toBeInTheDocument();
    expect(screen.queryByText("frontier_demo_disabled")).not.toBeInTheDocument();
  });
});
