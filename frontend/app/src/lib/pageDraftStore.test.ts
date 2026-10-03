import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { clearPageDrafts, draftKey, PAGE_DRAFT_DB, PAGE_DRAFT_TTL, MAX_DRAFT_BYTES, objectDraftCodec, preparePageDraft, readPageDraft, removePageDraft, writePageDrafts, draftError } from "./pageDraftStore";
import { initialProblemDraft, problemDraftCodec } from "./taskPageDrafts";
vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File);
beforeEach(async () => { await clearPageDrafts(); sessionStorage.clear(); });
afterEach(() => vi.restoreAllMocks());
const codec = objectDraftCodec<{ name: string; file: globalThis.File | null }>();
async function prepare(name = "saved", owner = "a", scope = "form") {
  const loaded = await readPageDraft(owner, scope, codec);
  return preparePageDraft(owner, scope, "server-1", { name, file: null }, codec, loaded.epoch, loaded.record?.revision ?? null);
}
async function mutateRecord(change: (row: Record<string, unknown>) => void) {
  const db = await new Promise<IDBDatabase>((resolve) => { const request = indexedDB.open(PAGE_DRAFT_DB, 1); request.onsuccess = () => resolve(request.result); });
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction("drafts", "readwrite"); const req = tx.objectStore("drafts").get(draftKey("a", "form"));
    req.onsuccess = () => { change(req.result); tx.objectStore("drafts").put(req.result); }; tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error);
  }); db.close();
}
describe("explicit IndexedDB draft transactions", () => {
  it("persists actual bytes and metadata, not blob URLs or filename placeholders", async () => {
    const data = initialProblemDraft(); data.sources[0].file = new File(["actual question bytes"], "question.txt", { type: "text/plain", lastModified: 123 }) as unknown as globalThis.File;
    data.sources[0].recognitionPages = "3-5"; data.sources[0].storedFileId = "owned-server-file";
    const loaded = await readPageDraft("a", "problem", problemDraftCodec);
    await writePageDrafts([preparePageDraft("a", "problem", "1", data, problemDraftCodec, loaded.epoch, null)]);
    const restored = (await readPageDraft("a", "problem", problemDraftCodec)).value!;
    expect(await restored.sources[0].file!.text()).toBe("actual question bytes");
    expect(restored.sources[0].file!.name).toBe("question.txt"); expect(restored.sources[0].file!.lastModified).toBe(123);
    expect(restored.sources[0].storedFileId).toBe("owned-server-file"); expect(restored.sources[0].recognitionPages).toBe("3-5");
    expect(Object.keys(localStorage).filter((key) => key.includes("draft"))).toEqual([]);
    expect(Object.keys(sessionStorage)).toEqual([]);
  });
  it("keeps the last explicit snapshot when later work is not saved", async () => {
    await writePageDrafts([await prepare()]); const unsaved = await prepare("unsaved");
    expect(unsaved.record.data).toEqual({ name: "unsaved", file: null });
    expect((await readPageDraft("a", "form", codec)).value?.name).toBe("saved");
  });
  it("isolates user, task, and workflow", async () => {
    await writePageDrafts([await prepare()]);
    expect((await readPageDraft("a", "other-task", codec)).value).toBeNull();
    expect((await readPageDraft("a", "other-workflow", codec)).value).toBeNull();
    expect((await readPageDraft("b", "form", codec)).value).toBeNull();
  });
  it("rejects a concurrent tab's stale snapshot and rolls back ALL fields/files", async () => {
    const tab1 = await prepare("tab1"); const tab2 = await prepare("tab2"); const another = await prepare("other", "a", "other");
    await writePageDrafts([tab1]);
    await expect(writePageDrafts([another, tab2])).rejects.toThrow("其他标签页");
    expect((await readPageDraft("a", "form", codec)).value?.name).toBe("tab1");
    expect((await readPageDraft("a", "other", codec)).value).toBeNull();
  });
  it("submit/delete tombstones fence late writes", async () => {
    await writePageDrafts([await prepare()]); const late = await prepare("late");
    await removePageDraft("a", "form"); await expect(writePageDrafts([late])).rejects.toThrow("其他标签页");
    expect((await readPageDraft("a", "form", codec)).value).toBeNull();
  });
  it("logout fences prepared writes and purges drafts", async () => {
    await writePageDrafts([await prepare()]); const late = await prepare("late");
    await clearPageDrafts(); await expect(writePageDrafts([late])).rejects.toThrow("登录状态");
    expect((await readPageDraft("a", "form", codec)).value).toBeNull();
  });
  it.each([0, 999])("rejects incompatible schema %s", async (version) => {
    await writePageDrafts([await prepare()]); await mutateRecord((row) => { row.version = version; });
    expect((await readPageDraft("a", "form", codec)).notice).toContain("版本不兼容");
  });
  it("expires snapshots and reports why they cannot restore", async () => {
    await writePageDrafts([await prepare()]); await mutateRecord((row) => { row.expires = Date.now() - 1; row.savedAt = Date.now() - PAGE_DRAFT_TTL - 1; });
    expect((await readPageDraft("a", "form", codec)).notice).toContain("过期");
  });
  it("discards corrupt actual file records instead of pretending to restore", async () => {
    await writePageDrafts([await prepare()]); await mutateRecord((row) => { row.files = [{ path: ["file"], blob: "not file bytes", name: "ghost.txt" }]; });
    expect((await readPageDraft("a", "form", codec)).notice).toContain("损坏");
  });
  it("rejects oversized snapshots before writing anything", async () => {
    const loaded = await readPageDraft("a", "form", codec);
    expect(() => preparePageDraft("a", "form", "1", { name: "large", file: new File([new Uint8Array(MAX_DRAFT_BYTES + 1)], "large.bin") as unknown as globalThis.File }, codec, loaded.epoch, null)).toThrow("64 MiB");
    expect((await readPageDraft("a", "form", codec)).value).toBeNull();
  });
  it("reports storage quota failure and leaves the old version intact", async () => {
    await writePageDrafts([await prepare()]); const next = await prepare("new");
    const put = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
    await expect(writePageDrafts([next])).rejects.toMatchObject({ name: "QuotaExceededError" }); put.mockRestore();
    expect((await readPageDraft("a", "form", codec)).value?.name).toBe("saved");
    expect(draftError(new DOMException("full", "QuotaExceededError"))).toContain("空间不足");
  });
  it("whitelists business data and never stores credentials", async () => {
    const loaded = await readPageDraft("a", "problem", problemDraftCodec);
    const data = { ...initialProblemDraft(), apiKey: "DO_NOT_STORE", password: "DO_NOT_STORE" };
    const snapshot = preparePageDraft("a", "problem", "1", data, problemDraftCodec, loaded.epoch, null);
    expect(JSON.stringify(snapshot.record)).not.toContain("DO_NOT_STORE");
  });
});
