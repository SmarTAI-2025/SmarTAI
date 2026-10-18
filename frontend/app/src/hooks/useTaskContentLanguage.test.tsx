import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { initializeContentLanguage } from "@/api/contentLanguage";
import { useTaskContentLanguage } from "./useTaskContentLanguage";

const state = vi.hoisted(() => ({ locale: "en-US" }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => state }));
vi.mock("@/api/contentLanguage", async original => ({ ...await original<object>(), initializeContentLanguage: vi.fn() }));

beforeEach(() => { vi.resetAllMocks(); state.locale = "en-US"; });

it("captures entry language before asynchronous initialization, keeps it on UI switches and uses the saved server value on remount", async () => {
  let finish!: (result: { language: "en" }) => void;
  vi.mocked(initializeContentLanguage).mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const { result, rerender, unmount } = renderHook(() => useTaskContentLanguage("task-1"));
  state.locale = "zh-CN"; rerender();
  await act(async () => finish({ language: "en" }));
  expect(result.current.language).toBe("en");
  expect(initializeContentLanguage).toHaveBeenCalledExactlyOnceWith("task-1", "en");
  unmount();
  vi.mocked(initializeContentLanguage).mockResolvedValue({ language: "en" });
  const restored = renderHook(() => useTaskContentLanguage("task-1"));
  await waitFor(() => expect(restored.result.current.language).toBe("en"));
  expect(initializeContentLanguage).toHaveBeenLastCalledWith("task-1", "zh");
});

it("retries failed initialization with the original language and does not initialize submissions", async () => {
  vi.mocked(initializeContentLanguage).mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ language: "en" });
  const { result, rerender } = renderHook(() => useTaskContentLanguage("task-1"));
  await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
  state.locale = "zh-CN"; rerender();
  act(() => result.current.retry());
  await waitFor(() => expect(result.current.language).toBe("en"));
  expect(initializeContentLanguage).toHaveBeenLastCalledWith("task-1", "en");
  renderHook(() => useTaskContentLanguage("task-2", false));
  expect(initializeContentLanguage).toHaveBeenCalledTimes(2);
});
