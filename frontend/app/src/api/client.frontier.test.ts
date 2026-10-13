import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, clearAuthToken, getAuthToken, setAuthToken } from "./client";
import { createFrontierDemoSession, getCurrentUser } from "./auth";
import { createTask } from "./tasks";
import { beginFrontierSession, clearFrontierToken, endFrontierSessionLocally, getFrontierToken, rememberFrontierTask } from "@/lib/frontierSession";
import { setSessionExpired } from "@/lib/sessionExpiry";

const originalAdapter = apiClient.defaults.adapter;
const response = (config: InternalAxiosRequestConfig, data: unknown) => ({ status: 200, data, headers: new AxiosHeaders(), config, statusText: "OK" });
const unauthorized = (config: InternalAxiosRequestConfig) => new AxiosError("Expired", AxiosError.ERR_BAD_REQUEST, config, undefined, { status: 401, data: {}, headers: new AxiosHeaders(), config, statusText: "Unauthorized" });

beforeEach(() => {
  sessionStorage.clear();
  setAuthToken("teacher-access");
  beginFrontierSession("guest-access", "guest-one");
  rememberFrontierTask("demo-task");
});
afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
  clearAuthToken();
  sessionStorage.clear();
  setSessionExpired(false);
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("coexisting teacher and Frontier sessions", () => {
  it("cancels a later workflow request after leaving instead of attaching the teacher login", async () => {
    const adapter = vi.fn();
    apiClient.defaults.adapter = adapter;
    endFrontierSessionLocally();
    await expect(apiClient.get("/tasks/demo-task/state")).rejects.toMatchObject({ code: "ERR_CANCELED" });
    expect(adapter).not.toHaveBeenCalled();
    expect(getAuthToken()).toBe("teacher-access");
  });
  it("creates a tab-local guest session without replacing teacher credentials or sending cookies", async () => {
    const calls: InternalAxiosRequestConfig[] = [];
    apiClient.defaults.adapter = async (config) => {
      calls.push(config);
      return response(config, { token: "guest-rotated", user: { id: "guest-one", role: "teacher" } });
    };
    await createFrontierDemoSession();
    expect(calls[0].withCredentials).toBe(false);
    expect(calls[0].headers.Authorization).toBe("Bearer guest-access");
    expect(getFrontierToken()).toBe("guest-rotated");
    expect(getAuthToken()).toBe("teacher-access");
  });

  it("keeps teacher auth calls teacher-scoped while browsing Demo and scopes task/analytics by owner", async () => {
    window.history.replaceState(null, "", "/frontier/live");
    const calls: InternalAxiosRequestConfig[] = [];
    apiClient.defaults.adapter = async (config) => { calls.push(config); return response(config, {}); };
    await getCurrentUser();
    await apiClient.post("/auth/activity");
    await apiClient.get("/tasks/teacher-task");
    window.history.replaceState(null, "", "/dashboard");
    await apiClient.get("/tasks/demo-task");
    await apiClient.get("/analytics/demo-task/per_question/q1");
    await apiClient.get("/tasks/teacher-task");
    expect(calls.map((config) => config.headers.Authorization)).toEqual([
      "Bearer teacher-access", "Bearer teacher-access", "Bearer teacher-access", "Bearer guest-access", "Bearer guest-access", "Bearer teacher-access",
    ]);
  });

  it("keeps a teacher background request and its refresh teacher-scoped after entering Demo", async () => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const calls: InternalAxiosRequestConfig[] = [];
    apiClient.defaults.adapter = async (config) => {
      calls.push(config);
      if (config.url === "/auth/refresh") return response(config, { token: "teacher-renewed" });
      if (config.headers.Authorization === "Bearer teacher-renewed") return response(config, {});
      await pending;
      throw unauthorized(config);
    };
    const result = apiClient.get("/tasks/teacher-task");
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    window.history.replaceState(null, "", "/frontier/live");
    finish();
    await result;
    expect(calls.map((config) => config.url)).toEqual(["/tasks/teacher-task", "/auth/refresh", "/tasks/teacher-task"]);
    expect(getAuthToken()).toBe("teacher-renewed");
    expect(getFrontierToken()).toBe("guest-access");
  });

  it("creates Demo tasks with an explicit guest credential and keeps ordinary creation teacher-scoped", async () => {
    const calls: InternalAxiosRequestConfig[] = [];
    apiClient.defaults.adapter = async (config) => { calls.push(config); return response(config, {}); };
    window.history.replaceState(null, "", "/frontier/live");
    await createTask({ name: "Guest", idempotencyKey: "guest", authScope: "frontier" });
    await createTask({ name: "Teacher", idempotencyKey: "teacher" });
    expect(calls.map((config) => config.headers.Authorization)).toEqual(["Bearer guest-access", "Bearer teacher-access"]);
    expect(JSON.parse(calls[0].data)).toEqual({ name: "Guest" });
  });

  it("renews a guest only through its dedicated endpoint after navigating away", async () => {
    const calls: string[] = [];
    apiClient.defaults.adapter = async (config) => {
      calls.push(config.url!);
      if (config.url === "/auth/frontier-demo-session") return response(config, { token: "guest-renewed" });
      if (config.headers.Authorization === "Bearer guest-renewed") return response(config, {});
      window.history.replaceState(null, "", "/login");
      throw unauthorized(config);
    };
    await apiClient.get("/tasks/demo-task");
    expect(calls).toEqual(["/tasks/demo-task", "/auth/frontier-demo-session", "/tasks/demo-task"]);
    expect(getAuthToken()).toBe("teacher-access");
  });

  it("does not replay an old guest request into a newly created guest session", async () => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const calls: string[] = [];
    apiClient.defaults.adapter = async (config) => { calls.push(config.url!); await pending; throw unauthorized(config); };
    const result = apiClient.get("/tasks/demo-task").catch((error: unknown) => error);
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    beginFrontierSession("another-guest", "guest-two");
    finish();
    await expect(result).resolves.toMatchObject({ code: "ERR_CANCELED" });
    expect(calls).toEqual(["/tasks/demo-task"]);
    expect(getFrontierToken()).toBe("another-guest");
    expect(getAuthToken()).toBe("teacher-access");
  });

  it("expires only the guest and permits an explicit new session attempt", async () => {
    apiClient.defaults.adapter = async (config) => { throw unauthorized(config); };
    await expect(createFrontierDemoSession()).rejects.toMatchObject({ status: 401 });
    expect(getFrontierToken()).toBeNull();
    expect(getAuthToken()).toBe("teacher-access");
    const calls: InternalAxiosRequestConfig[] = [];
    apiClient.defaults.adapter = async (config) => { calls.push(config); return response(config, { token: "new-guest", user: { id: "guest-two" } }); };
    await createFrontierDemoSession();
    expect(calls[0].headers.Authorization).toBeUndefined();
    expect(calls[0].withCredentials).toBe(false);
    expect(getFrontierToken()).toBe("new-guest");
    clearFrontierToken();
    expect(getAuthToken()).toBe("teacher-access");
  });
});
