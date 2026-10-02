import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { RouteRecoveryPage } from "./RouteRecoveryPage";

vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN" }) }));

it("offers non-mutating recovery without exposing an exception payload", () => {
  const silenceExpectedError = vi.spyOn(console, "error").mockImplementation(() => {});
  function BrokenPage(): never { throw new Error("private response detail"); }
  try {
    const router = createMemoryRouter([{ path: "/", element: <BrokenPage />, errorElement: <RouteRecoveryPage /> }]);
    render(<RouterProvider router={router} />);
    expect(screen.getByRole("button", { name: "重新加载页面" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回历史任务" })).toHaveAttribute("href", "/history");
    expect(screen.queryByText(/private response detail/)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("不会自动重新识别或批改");
  } finally {
    silenceExpectedError.mockRestore();
  }
});
