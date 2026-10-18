import { beforeEach, expect, it, vi } from "vitest";
import { putJSON } from "./client";
import { initializeContentLanguage } from "./contentLanguage";

vi.mock("./client", () => ({ putJSON: vi.fn() }));
beforeEach(() => { vi.resetAllMocks(); sessionStorage.clear(); });

it("keeps the first entry language through failed requests, UI changes and a module reload", async () => {
  vi.mocked(putJSON).mockRejectedValueOnce(new Error("offline"));
  await expect(initializeContentLanguage("task-1", "en")).rejects.toThrow("offline");
  vi.resetModules();
  const restored = await import("./contentLanguage");
  vi.mocked(putJSON).mockResolvedValue({ language: "en" });
  await expect(restored.initializeContentLanguage("task-1", "zh")).resolves.toEqual({ language: "en" });
  expect(putJSON).toHaveBeenLastCalledWith("/tasks/task-1/content-language/default", { language: "en" });
  expect(sessionStorage.getItem("smartai_content_language_pending:task-1")).toBeNull();
  // Another task gets its own first-entry language; saved server values win.
  await restored.initializeContentLanguage("task-2", "zh");
  expect(putJSON).toHaveBeenLastCalledWith("/tasks/task-2/content-language/default", { language: "zh" });
});

it("still uses the durable server value when browser storage is unavailable", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementationOnce(() => { throw new Error("blocked"); });
  vi.mocked(putJSON).mockResolvedValue({ language: "zh" });
  await expect(initializeContentLanguage("task-1", "en")).resolves.toEqual({ language: "zh" });
  expect(putJSON).toHaveBeenLastCalledWith("/tasks/task-1/content-language/default", { language: "en" });
});
