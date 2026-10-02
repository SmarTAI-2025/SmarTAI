import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, clearAuthToken, getAuthToken, getJSON, setAuthToken } from "@/api/client";
import { authKeys } from "@/api/hooks/keys";
import { setSessionExpired } from "@/lib/sessionExpiry";
import { LoginPage } from "@/routes/LoginPage";
import { RequireTeacherSession } from "./RequireTeacherSession";

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

const teacher = { id: "teacher-1", username: "test", role: "teacher", is_active: true, created_at: 1 };
const originalAdapter = apiClient.defaults.adapter;

function fail(config: InternalAxiosRequestConfig, status: number): never {
  throw new AxiosError("Request failed", AxiosError.ERR_BAD_REQUEST, config, undefined, {
    data: { detail: status === 401 ? "Refresh session missing" : "Service unavailable" },
    status, statusText: "Error", headers: new AxiosHeaders(), config,
  });
}
function ok(config: InternalAxiosRequestConfig, data: unknown) {
  return { data, status: 200, statusText: "OK", headers: new AxiosHeaders(), config };
}
function History() {
  const location = useLocation();
  return <>
    <div>Signed-in history {location.pathname}{location.search}{location.hash}</div>
    <button onClick={() => { void getJSON("/tasks/").catch(() => {}); }}>Load history</button>
  </>;
}
function renderSession(cached = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  if (cached) client.setQueryData(authKeys.me, teacher);
  client.setQueryData(["tasks", "private"], { secretTask: "fixture" });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/history?q=algebra#results"]}>
    <Routes>
      <Route path="/history" element={<RequireTeacherSession><History /></RequireTeacherSession>} />
      <Route path="/login" element={<LoginPage />} />
    </Routes>
  </MemoryRouter></QueryClientProvider>);
  return client;
}

beforeEach(() => { setAuthToken("expired-token"); });
afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
  clearAuthToken();
  setSessionExpired(false);
  vi.restoreAllMocks();
});

describe("live session expiry", () => {
  it("leaves cached authenticated UI, clears private cache, explains expiry and returns after signing in", async () => {
    const calls: string[] = [];
    apiClient.defaults.adapter = async (config) => {
      calls.push(config.url ?? "");
      if (config.url === "/auth/login") return ok(config, { token: "new-login", user: teacher });
      return fail(config, 401);
    };
    const client = renderSession();
    fireEvent.click(screen.getByText("Load history"));
    expect(await screen.findByRole("alert")).toHaveTextContent("登录状态已过期，请重新登录。");
    expect(screen.queryByText(/Signed-in history/)).not.toBeInTheDocument();
    expect(getAuthToken()).toBeNull();
    expect(client.getQueryData(authKeys.me)).toBeUndefined();
    expect(client.getQueryData(["tasks", "private"])).toBeUndefined();
    expect(calls).toEqual(["/tasks/", "/auth/refresh"]);

    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText("输入用户名"), "test");
    await user.type(screen.getByPlaceholderText("输入密码"), "test-password");
    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByText(/Signed-in history/)).toHaveTextContent("/history?q=algebra#results");
    expect(getAuthToken()).toBe("new-login");
    expect(calls.filter((url) => url === "/auth/refresh")).toHaveLength(1);
  });

  it("keeps a mounted authenticated workspace when refresh temporarily fails with 503", async () => {
    apiClient.defaults.adapter = async (config) => fail(config, config.url === "/auth/refresh" ? 503 : 401);
    const client = renderSession();
    await expect(getJSON("/tasks/")).rejects.toMatchObject({ status: 503 });
    expect(screen.getByText(/Signed-in history/)).toBeInTheDocument();
    expect(getAuthToken()).toBe("expired-token");
    expect(client.getQueryData(authKeys.me)).toEqual(teacher);
  });

  it("offers retry instead of falsely reporting expiry when initial session checking is offline", async () => {
    const adapter = vi.fn(async (config) => {
      throw new AxiosError("Network Error", AxiosError.ERR_NETWORK, config);
    });
    apiClient.defaults.adapter = adapter;
    renderSession(false);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法确认登录状态");
    expect(getAuthToken()).toBe("expired-token");
    apiClient.defaults.adapter = async (config) => ok(config, teacher);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getByText(/Signed-in history/)).toBeInTheDocument());
  });
});
