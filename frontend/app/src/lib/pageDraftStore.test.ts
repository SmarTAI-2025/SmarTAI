import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { clearPageDrafts, draftKey, PAGE_DRAFT_TTL, readPageDraft, writePageDraft } from "./pageDraftStore";
import { initialProblemDraft, problemDraftCodec } from "./taskPageDrafts";

beforeEach(() => { clearPageDrafts(); sessionStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });
const key = draftKey("teacher-a", "problems:task-a:0");
function save() {
  const draft = initialProblemDraft();
  draft.sources[0].file = new File(["PRIVATE_DOCUMENT_BYTES"], "questions.txt");
  draft.sources[0].recognitionPages = "3-5";
  draft.sources[0].storedFileId = "stored-reference";
  expect(writePageDraft("teacher-a", "problems:task-a:0", draft, problemDraftCodec)).toBe(true);
  return draft;
}
function reload() {
  const raw = sessionStorage.getItem(key)!;
  clearPageDrafts();
  sessionStorage.setItem(key, raw);
}

describe("tab-scoped draft lifecycle", () => {
  it("retains a File in memory but persists only whitelisted metadata", () => {
    const draft = save();
    expect(readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).value?.sources[0].file).toBe(draft.sources[0].file);
    const raw = sessionStorage.getItem(key)!;
    expect(raw).not.toContain("PRIVATE_DOCUMENT_BYTES");
    expect(localStorage.getItem(key)).toBeNull();
    reload();
    const source = readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).value!.sources[0];
    expect(source.file).toBeNull();
    expect(source.fileName).toBe("questions.txt");
    expect(source.storedFileId).toBe("stored-reference");
    expect(source.recognitionPages).toBe("3-5");
  });

  it("separates owner, task, course/revision and workflow keys", () => {
    save();
    for (const [owner, scope] of [["teacher-b", "problems:task-a:0"], ["teacher-a", "problems:task-b:0"], ["teacher-a", "problems:task-a:1"], ["teacher-a", "submissions:task-a:0"]]) {
      expect(readPageDraft(owner, scope, problemDraftCodec).value).toBeNull();
    }
  });

  it.each(["broken-json", JSON.stringify({ version: 0 }), JSON.stringify({ version: 999 })])("discards broken/old schemas: %s", (raw) => {
    sessionStorage.setItem(key, raw);
    expect(readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).notice).toBe("discarded");
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it("rejects structurally invalid nested fields", () => {
    save(); reload();
    const data = JSON.parse(sessionStorage.getItem(key)!);
    data.data.sources[0].sourceMode = "credentials";
    sessionStorage.setItem(key, JSON.stringify(data));
    expect(readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).value).toBeNull();
  });

  it("expires both memory and session snapshots after 8 hours", () => {
    vi.useFakeTimers(); save(); vi.advanceTimersByTime(PAGE_DRAFT_TTL + 1);
    expect(readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).notice).toBe("discarded");
  });

  it("falls back to latest in-memory work and removes stale storage on write failure", () => {
    const draft = save();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("quota", "QuotaExceededError"); });
    draft.sources[0].recognitionPages = "7";
    expect(writePageDraft("teacher-a", "problems:task-a:0", draft, problemDraftCodec)).toBe(false);
    expect(sessionStorage.getItem(key)).toBeNull();
    expect(readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).value?.sources[0].recognitionPages).toBe("7");
  });

  it("ignores extra secret fields rather than serializing them", () => {
    const draft = { ...initialProblemDraft(), apiKey: "NEVER_STORE_THIS", password: "NEVER_STORE_THIS" };
    writePageDraft("teacher-a", "problems:task-a:0", draft, problemDraftCodec);
    expect(sessionStorage.getItem(key)).not.toContain("NEVER_STORE_THIS");
  });

  it("clears all owners on logout even when browser storage is blocked", () => {
    save();
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => clearPageDrafts()).not.toThrow();
    vi.restoreAllMocks();
    clearPageDrafts();
    expect(readPageDraft("teacher-a", "problems:task-a:0", problemDraftCodec).value).toBeNull();
  });
});
