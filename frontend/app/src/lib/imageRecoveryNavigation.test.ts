import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearPageDrafts } from "./pageDraftStore";
import { rememberImageReturn, takeImageReturn } from "./imageRecoveryNavigation";

const path = "/tasks/a/upload/problems";
beforeEach(() => sessionStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("explicit image recovery choices", () => {
  it("isolates owners and task steps, and consumes the choice once", () => {
    rememberImageReturn("teacher-a", path, "new-model");
    expect(takeImageReturn("teacher-b", path)).toBeNull();
    expect(takeImageReturn("teacher-a", "/tasks/a/submissions/upload")).toBeNull();
    expect(takeImageReturn("teacher-a", path)).toBe("new-model");
    expect(takeImageReturn("teacher-a", path)).toBeNull();
  });

  it("does not restore expired choices", () => {
    rememberImageReturn("teacher-a", path, "old-model");
    const later = Date.now() + 31 * 60_000;
    vi.spyOn(Date, "now").mockReturnValue(later);
    expect(takeImageReturn("teacher-a", path)).toBeNull();
  });

  it("logout and account changes clear pending navigation choices with drafts", async () => {
    rememberImageReturn("teacher-a", path, "new-model");
    await clearPageDrafts();
    expect(takeImageReturn("teacher-a", path)).toBeNull();
    expect(Object.keys(sessionStorage)).toEqual([]);
  });
});
