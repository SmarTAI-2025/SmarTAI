import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, clearAuthToken, getAuthToken, setAuthToken } from "./client";
import { setSessionExpired } from "@/lib/sessionExpiry";

const originalAdapter = apiClient.defaults.adapter;
function failure(config: InternalAxiosRequestConfig, status = 401) {
  return new AxiosError("Failed", AxiosError.ERR_BAD_REQUEST, config, undefined, {
    status, data: { detail: "Refresh session missing" }, headers: new AxiosHeaders(), config, statusText: "Failed",
  });
}
function response(config: InternalAxiosRequestConfig, data: unknown) {
  return { status: 200, data, headers: new AxiosHeaders(), config, statusText: "OK" };
}
beforeEach(() => setAuthToken("old-access"));
afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
  clearAuthToken();
  setSessionExpired(false);
  vi.restoreAllMocks();
});

describe("session refresh boundary", () => {
  it("shares one refresh across concurrent requests and replays each only once", async () => {
    let complete!: () => void;
    const pending = new Promise<void>((resolve) => { complete = resolve; });
    const calls: string[] = [];
    apiClient.defaults.adapter = async (config) => {
      calls.push(config.url ?? "");
      if (config.url === "/auth/refresh") { await pending; return response(config, { token: "rotated" }); }
      if (config.headers.Authorization === "Bearer rotated") return response(config, { ok: true });
      throw failure(config);
    };
    const first = apiClient.get("/tasks/");
    const second = apiClient.get("/experts/");
    await vi.waitFor(() => expect(calls).toContain("/auth/refresh"));
    complete();
    await Promise.all([first, second]);
    expect(calls.filter((url) => url === "/auth/refresh")).toHaveLength(1);
    expect(calls.filter((url) => url === "/tasks/")).toHaveLength(2);
    expect(calls.filter((url) => url === "/experts/")).toHaveLength(2);
  });

  it.each([true, false])("an old refresh cannot replace or clear a later login (success=%s)", async (success) => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const called = vi.fn();
    apiClient.defaults.adapter = async (config) => {
      if (config.url !== "/auth/refresh") throw failure(config);
      called();
      await pending;
      if (!success) throw failure(config);
      return response(config, { token: "old-session-rotation" });
    };
    const result = apiClient.get("/tasks/").catch((error: unknown) => error);
    await vi.waitFor(() => expect(called).toHaveBeenCalled());
    setAuthToken("new-login");
    finish();
    await result;
    expect(getAuthToken()).toBe("new-login");
  });

  it("stops when a replay with the renewed token is still unauthorized", async () => {
    const refresh = vi.fn();
    apiClient.defaults.adapter = async (config) => {
      if (config.url === "/auth/refresh") { refresh(); return response(config, { token: "rotated" }); }
      throw failure(config);
    };
    await expect(apiClient.get("/tasks/")).rejects.toBeInstanceOf(AxiosError);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(getAuthToken()).toBeNull();
  });
});
