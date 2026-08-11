import { render, screen } from "@testing-library/react";
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

    render(<I18nProvider><RouterProvider router={router} /></I18nProvider>);

    expect(screen.getByRole("heading", { name: "Entering the live demo" })).toBeInTheDocument();
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

    render(<I18nProvider><RouterProvider router={router} /></I18nProvider>);

    expect(await screen.findByText(/此环境尚未启用 Demo 服务/)).toBeInTheDocument();
    expect(screen.getByText("真实产品 Demo")).toBeInTheDocument();
    expect(document.querySelector('[data-smartai-app-mark="silver"]')).toBeInTheDocument();
    expect(document.querySelector('[data-smartai-wordmark="white"]')).toBeInTheDocument();
    expect(screen.queryByText("frontier_demo_disabled")).not.toBeInTheDocument();
  });
});
