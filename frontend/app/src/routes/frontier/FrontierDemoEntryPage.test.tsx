import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createFrontierDemoSession } from "@/api/auth";
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
});
