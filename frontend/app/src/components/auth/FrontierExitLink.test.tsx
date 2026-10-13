import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as client from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { FrontierContext } from "@/lib/frontierContext";
import { beginFrontierSession, getFrontierToken, isFrontierTaskPath, rememberFrontierTask } from "@/lib/frontierSession";
import { DraftLeaveProvider, useDraftLeave, type DraftController } from "@/hooks/useDraftLeave";
import { FrontierExitLink } from "./FrontierExitLink";

beforeEach(() => {
  sessionStorage.clear();
  localStorage.setItem("smartai_locale", "zh-CN");
  client.setAuthToken("teacher-session");
  beginFrontierSession("guest-session", "guest-owner");
  rememberFrontierTask("guest-task");
  vi.spyOn(window, "alert").mockImplementation(() => {});
});
afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear(); localStorage.clear(); });

function UnsavedDemo() {
  const { register } = useDraftLeave();
  useEffect(() => register(() => ({ id: Symbol.for("demo-dirty"), scope: "demo", active: true, dirty: true, busy: true,
    loaded: true, secret: false, savedAt: null, notice: null, hasConflict: false,
    discard: () => {}, remove: async () => {}, restore: () => {}, keepCurrent: () => {}, prepare: () => { throw new Error("No save"); },
  } satisfies DraftController)), [register]);
  return <FrontierExitLink to="/login">注册/登录</FrontierExitLink>;
}

function setup(guestContext = true) {
  const queries = new QueryClient();
  queries.setQueryData(["cached-progress"], "private");
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /></DraftLeaveProvider>, children: [
    { path: "/frontier/live", element: <UnsavedDemo /> }, { path: "/login", element: <p>登录页</p> },
  ] }], { initialEntries: ["/frontier/live"] });
  render(<QueryClientProvider client={queries}><I18nProvider><FrontierContext.Provider value={guestContext}><RouterProvider router={router} /></FrontierContext.Provider></I18nProvider></QueryClientProvider>);
  return { router, queries };
}

it("keeps progress and avoids a request when leaving is cancelled", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const end = vi.spyOn(client, "postJSON").mockResolvedValue({ status: "ended" });
  const { router } = setup();
  fireEvent.click(screen.getByRole("link", { name: "注册/登录" }));
  expect(confirm).toHaveBeenCalledWith("本次 Demo 进度不会保留，是否确认离开？");
  expect(end).not.toHaveBeenCalled();
  expect(router.state.location.pathname).toBe("/frontier/live");
  expect(getFrontierToken()).toBe("guest-session");
});

it("ends the guest before leaving and bypasses the already-confirmed draft prompt", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  const end = vi.spyOn(client, "postJSON").mockImplementation(async () => { await pending; return { status: "ended" }; });
  const { router, queries } = setup();
  fireEvent.click(screen.getByRole("link", { name: "注册/登录" }));
  expect(router.state.location.pathname).toBe("/frontier/live");
  await act(async () => finish());
  expect(await screen.findByText("登录页")).toBeInTheDocument();
  expect(end).toHaveBeenCalledWith("/auth/frontier-demo-session/end", {}, { _authScope: "frontier", _skipAuthRefresh: true });
  expect(getFrontierToken()).toBeNull();
  expect(isFrontierTaskPath("/tasks/guest-task")).toBe(false);
  expect(client.getAuthToken()).toBe("teacher-session");
  expect(queries.getQueryData(["cached-progress"])).toBeUndefined();
  expect(screen.queryByText("有未暂存修改")).not.toBeInTheDocument();
});

it.each([401, 503])("handles an expired or unavailable end request (%s)", async (status) => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(client, "postJSON").mockRejectedValue(new client.APIError(status, "Failed"));
  const { router } = setup();
  fireEvent.click(screen.getByRole("link", { name: "注册/登录" }));
  await waitFor(() => status === 401 ? expect(router.state.location.pathname).toBe("/login") : expect(window.alert).toHaveBeenCalled());
  expect(client.getAuthToken()).toBe("teacher-session");
  if (status === 503) {
    expect(router.state.location.pathname).toBe("/frontier/live");
    expect(getFrontierToken()).toBe("guest-session");
  }
});

it("does not clear the outer teacher query cache on the standalone entry page", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(client, "postJSON").mockResolvedValue({ status: "ended" });
  const { queries } = setup(false);
  fireEvent.click(screen.getByRole("link", { name: "注册/登录" }));
  await screen.findByText("登录页");
  expect(queries.getQueryData(["cached-progress"])).toBe("private");
});
