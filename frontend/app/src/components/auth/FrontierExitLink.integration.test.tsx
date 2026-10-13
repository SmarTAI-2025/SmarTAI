import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import { useEffect } from "react";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiClient, getAuthToken, setAuthToken, type AuthAwareRequestConfig } from "@/api/client";
import { DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { I18nProvider } from "@/i18n/I18nProvider";
import { beginFrontierSession, clearFrontierToken, getFrontierToken, isFrontierExitPending, isFrontierTaskPath, rememberFrontierTask, setFrontierExitPending } from "@/lib/frontierSession";
import { FrontierDemoEntryPage } from "@/routes/frontier/FrontierDemoEntryPage";
import { FrontierExitLink } from "./FrontierExitLink";
import { WorkspaceSession } from "./WorkspaceSession";

const originalAdapter = apiClient.defaults.adapter;
const guest = { id: "guest-owner", username: "Guest", role: "teacher", is_active: true };
const response = (config: InternalAxiosRequestConfig, data: unknown) => ({ status: 200, data, headers: new AxiosHeaders(), config, statusText: "OK" });
const failure = (config: InternalAxiosRequestConfig, status: number) => new AxiosError("Failed", AxiosError.ERR_BAD_REQUEST, config, undefined, { status, data: {}, headers: new AxiosHeaders(), config, statusText: "Failed" });
function deferred() { let resolve!: () => void; const promise = new Promise<void>((done) => { resolve = done; }); return { promise, resolve }; }

beforeEach(() => {
  sessionStorage.clear();
  localStorage.setItem("smartai_locale", "zh-CN");
  setAuthToken("teacher-session");
  beginFrontierSession("guest-session", guest.id);
  rememberFrontierTask("guest-task");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "alert").mockImplementation(() => {});
});
afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
  setFrontierExitPending(false);
  sessionStorage.clear();
  localStorage.clear();
  vi.restoreAllMocks();
});

function setup(initialPath = "/frontier/live", destination = "/login") {
  const teacherQueries = new QueryClient();
  teacherQueries.setQueryData(["teacher-private-library"], "teacher-data");
  let guestQueries: QueryClient | undefined;
  function Demo() {
    const queries = useQueryClient();
    useEffect(() => { guestQueries = queries; queries.setQueryData(["demo-progress"], "guest-data"); }, [queries]);
    return <FrontierExitLink to={destination}>离开 Demo</FrontierExitLink>;
  }
  // Match main's public routes and pathless workspace guard. The entry page,
  // auth hooks, Axios interceptors and session stores are intentionally real.
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /></DraftLeaveProvider>, children: [
    { path: "/", element: <p>公开宣传页</p> },
    { path: "/login", element: <p>公开登录页</p> },
    { path: "/frontier/enter", element: <FrontierDemoEntryPage /> },
    { element: <WorkspaceSession><Outlet /></WorkspaceSession>, children: [
      { path: "/frontier/live", element: <Demo /> },
      { path: "/tasks/:taskId/questions", element: <Demo /> },
    ] },
  ] }], { initialEntries: [initialPath] });
  render(<QueryClientProvider client={teacherQueries}><I18nProvider><RouterProvider router={router} /></I18nProvider></QueryClientProvider>);
  return { router, teacherQueries, getGuestQueries: () => guestQueries! };
}

it.each([
  ["/frontier/live", "/login"], ["/frontier/live", "/"],
  ["/tasks/guest-task/questions", "/login"], ["/tasks/guest-task/questions", "/"],
])("leaves %s for %s without the real guard reopening a Demo", async (initialPath, destination) => {
  const calls: InternalAxiosRequestConfig[] = [];
  let ended = false;
  apiClient.defaults.adapter = async (config) => {
    calls.push(config);
    if (config.url === "/auth/frontier-demo-session/end") { ended = true; return response(config, { status: "ended" }); }
    if (config.url === "/auth/frontier-demo-session") return response(config, { token: "unwanted-new-guest", user: { ...guest, id: "new-owner" } });
    if (ended) throw failure(config, 401);
    return response(config, guest);
  };
  const { router, teacherQueries, getGuestQueries } = setup(initialPath, destination);
  fireEvent.click(await screen.findByRole("link", { name: "离开 Demo" }));
  await screen.findByText(destination === "/login" ? "公开登录页" : "公开宣传页");
  await act(async () => {});
  expect(router.state.location.pathname).toBe(destination);
  expect(calls.map((config) => config.url)).toEqual(["/auth/me", "/auth/frontier-demo-session/end"]);
  expect(calls.every((config) => config.headers.Authorization === "Bearer guest-session" && config.withCredentials === false)).toBe(true);
  expect(getFrontierToken()).toBeNull();
  expect(isFrontierTaskPath("/tasks/guest-task/questions")).toBe(false);
  expect(getGuestQueries().getQueryCache().getAll()).toHaveLength(0);
  expect(getAuthToken()).toBe("teacher-session");
  expect(teacherQueries.getQueryData(["teacher-private-library"])).toBe("teacher-data");
});

it("does not renew a pending guest request after end starts or reopen the guard", async () => {
  const background = deferred(); const end = deferred();
  const calls: InternalAxiosRequestConfig[] = [];
  apiClient.defaults.adapter = async (config) => {
    calls.push(config);
    if (config.url === "/tasks/guest-task/state") { await background.promise; throw failure(config, 401); }
    if (config.url === "/auth/frontier-demo-session/end") { await end.promise; return response(config, { status: "ended" }); }
    return response(config, guest);
  };
  const { router, getGuestQueries } = setup("/tasks/guest-task/questions");
  const link = await screen.findByRole("link", { name: "离开 Demo" });
  const pending = getGuestQueries().fetchQuery({ queryKey: ["pending-task"], queryFn: () => apiClient.get("/tasks/guest-task/state") }).catch(() => {});
  await waitFor(() => expect(calls).toHaveLength(2));
  fireEvent.click(link);
  await waitFor(() => expect(calls).toHaveLength(3));
  await act(async () => { background.resolve(); await pending; });
  expect(router.state.location.pathname).toBe("/tasks/guest-task/questions");
  expect(getFrontierToken()).toBe("guest-session");
  await act(async () => end.resolve());
  await screen.findByText("公开登录页");
  expect(calls.map((config) => config.url)).toEqual(["/auth/me", "/tasks/guest-task/state", "/auth/frontier-demo-session/end"]);
  expect(getAuthToken()).toBe("teacher-session");
});

it.each([401, 503])("handles end status %s without creating another guest session", async (status) => {
  const calls: InternalAxiosRequestConfig[] = [];
  let attempt = 0;
  apiClient.defaults.adapter = async (config) => {
    calls.push(config);
    if (config.url === "/auth/frontier-demo-session/end" && attempt++ === 0) throw failure(config, status);
    return response(config, guest);
  };
  const { router } = setup();
  fireEvent.click(await screen.findByRole("link", { name: "离开 Demo" }));
  if (status === 503) {
    await waitFor(() => expect(window.alert).toHaveBeenCalled());
    expect(router.state.location.pathname).toBe("/frontier/live");
    expect(isFrontierExitPending()).toBe(false);
    expect(getFrontierToken()).toBe("guest-session");
    fireEvent.click(screen.getByRole("link", { name: "离开 Demo" }));
  }
  await screen.findByText("公开登录页");
  expect(calls.filter((config) => config.url === "/auth/frontier-demo-session")).toHaveLength(0);
  expect(calls.filter((config) => config.url?.endsWith("/end")).every((config) => (config as AuthAwareRequestConfig)._skipAuthRefresh)).toBe(true);
  expect(getAuthToken()).toBe("teacher-session");
});

it.each([true, false])("leaves entry while its session request is pending (existing guest: %s)", async (existingGuest) => {
  if (!existingGuest) clearFrontierToken();
  const opening = deferred(); const end = deferred();
  const calls: InternalAxiosRequestConfig[] = [];
  apiClient.defaults.adapter = async (config) => {
    calls.push(config);
    if (config.url === "/auth/frontier-demo-session") { await opening.promise; return response(config, { token: "late-guest", user: guest }); }
    if (config.url === "/auth/frontier-demo-session/end") await end.promise;
    return response(config, { status: "ended" });
  };
  const { router, teacherQueries } = setup("/frontier/enter");
  await waitFor(() => expect(calls).toHaveLength(1));
  fireEvent.click(screen.getByRole("link", { name: "返回项目展示" }));
  if (existingGuest) {
    await waitFor(() => expect(calls).toHaveLength(2));
    await act(async () => opening.resolve());
    expect(router.state.location.pathname).toBe("/frontier/enter");
    expect(getFrontierToken()).toBe("guest-session");
    await act(async () => end.resolve());
  }
  await screen.findByText("公开宣传页");
  await act(async () => opening.resolve());
  expect(router.state.location.pathname).toBe("/");
  expect(getFrontierToken()).toBeNull();
  expect(calls.filter((config) => config.url === "/auth/frontier-demo-session")).toHaveLength(1);
  expect(calls.filter((config) => config.url === "/auth/me")).toHaveLength(0);
  expect(getAuthToken()).toBe("teacher-session");
  expect(teacherQueries.getQueryData(["teacher-private-library"])).toBe("teacher-data");
});
