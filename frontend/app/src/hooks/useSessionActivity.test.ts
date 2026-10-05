import { AxiosError, AxiosHeaders } from "axios";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiClient, clearAuthToken, getAuthToken, SESSION_ACTIVITY_KEY, setAuthToken } from "@/api/client";
import { isSessionExpired, setSessionExpired } from "@/lib/sessionExpiry";
import { SESSION_IDLE_MS, startSessionActivity } from "./useSessionActivity";

const adapter = apiClient.defaults.adapter;
let stop = () => {};
const calls: string[] = [];
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-04T15:00:00Z"));
  calls.length = 0; setAuthToken("initial");
  apiClient.defaults.adapter = async config => {
    calls.push(config.url!);
    return { status: 200, statusText: "OK", config, headers: new AxiosHeaders(), data: { token: "renewed" } };
  };
});
afterEach(() => { stop(); clearAuthToken(); setSessionExpired(false); apiClient.defaults.adapter = adapter; vi.useRealTimers(); });

it("expires after 30 idle minutes even when polling, focus and visibility events continue", async () => {
  stop = startSessionActivity();
  await vi.advanceTimersByTimeAsync(SESSION_IDLE_MS - 1);
  window.dispatchEvent(new Event("focus")); document.dispatchEvent(new Event("visibilitychange"));
  expect(isSessionExpired()).toBe(false); expect(calls).toEqual([]);
  await vi.advanceTimersByTimeAsync(1);
  expect(isSessionExpired()).toBe(true); expect(getAuthToken()).toBeNull();
});

it("continues past the original 30-minute login deadline while the user edits", async () => {
  stop = startSessionActivity();
  for (let minute = 0; minute < 60; minute += 10) {
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(isSessionExpired()).toBe(false);
  }
  expect(calls).toEqual(Array(6).fill("/auth/activity"));
  await vi.advanceTimersByTimeAsync(SESSION_IDLE_MS);
  expect(isSessionExpired()).toBe(true);
});

it("does not reset the inactivity clock when remounting or waking after sleep", async () => {
  localStorage.setItem(SESSION_ACTIVITY_KEY, String(Date.now() - SESSION_IDLE_MS));
  stop = startSessionActivity(); window.dispatchEvent(new MouseEvent("pointerdown"));
  await vi.advanceTimersByTimeAsync(0);
  expect(calls).toEqual([]); expect(isSessionExpired()).toBe(true);
});

it("retains unsent user activity across route remounts", async () => {
  stop = startSessionActivity(); await vi.advanceTimersByTimeAsync(1000);
  window.dispatchEvent(new KeyboardEvent("keydown")); await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(2000);
  window.dispatchEvent(new KeyboardEvent("keydown"));
  expect(calls).toHaveLength(1); stop(); stop = startSessionActivity();
  await vi.advanceTimersByTimeAsync(0);
  expect(calls).toHaveLength(2);
});

it("keeps an active session on a temporary renewal failure and retries with a bound", async () => {
  apiClient.defaults.adapter = async config => {
    calls.push(config.url!);
    throw new AxiosError("offline", AxiosError.ERR_NETWORK, config);
  };
  stop = startSessionActivity(); await vi.advanceTimersByTimeAsync(1000);
  window.dispatchEvent(new KeyboardEvent("keydown")); await vi.advanceTimersByTimeAsync(0);
  expect(getAuthToken()).toBe("initial"); expect(isSessionExpired()).toBe(false);
  await vi.advanceTimersByTimeAsync(30_000); expect(calls).toHaveLength(1);
});
