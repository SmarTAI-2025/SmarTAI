import { draftErrorCode, draftMessage } from "@/lib/draftMessages";
/** Explicit device-local snapshots. This module never calls a business API. */
export const PAGE_DRAFT_PREFIX = "smartai:page-draft:"; // obsolete automatic v1 cache, never read
export const PAGE_DRAFT_DB = "smartai-explicit-drafts";
export const PAGE_DRAFT_TTL = 7 * 24 * 60 * 60 * 1000;
export const MAX_DRAFT_BYTES = 64 * 1024 * 1024;
export const MAX_TOTAL_BYTES = 128 * 1024 * 1024;
export const MAX_DRAFTS = 30;
export interface DraftCodec<T> { encode(value: T): unknown; decode(value: unknown): T | null }
export type DraftRecord = {
  key: string; version: 2; owner: string; scope: string; businessVersion: string;
  revision: string; epoch: string; savedAt: number; expires: number; bytes: number; deleted?: boolean;
  data: unknown; files: Array<{ path: string[]; blob?: Blob; bytes?: ArrayBuffer; checksum?: number; type?: string; name: string; modified: number }>;
};
export type DraftLoad<T> = { value: T | null; record: DraftRecord | null; epoch: string; notice: string | null };
export type DraftWrite = { record: DraftRecord; expectedRevision: string | null; generation: number };
let generation = 0;
let connection: Promise<IDBDatabase> | null = null;
const events = new EventTarget();
const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("smartai-draft-lifecycle-v2");
channel?.addEventListener("message", (event) => {
  if (event.data?.type === "clear") generation++;
  events.dispatchEvent(new CustomEvent("change", { detail: { ...event.data, remote: true } }));
});
export function subscribeDraftChanges(listener: (event: Event) => void) {
  events.addEventListener("change", listener); return () => events.removeEventListener("change", listener);
}
function announce(detail: { type: string; key?: string }) {
  events.dispatchEvent(new CustomEvent("change", { detail })); channel?.postMessage(detail);
}
// Detect accidental byte corruption; this is integrity feedback, not encryption.
function fileChecksum(bytes: ArrayBuffer): number {
  let checksum = 2166136261;
  for (const byte of new Uint8Array(bytes)) checksum = Math.imul(checksum ^ byte, 16777619);
  return checksum >>> 0;
}
export function draftGeneration() { return generation; }
export function draftKey(owner: string, scope: string) { return JSON.stringify([owner, scope]); }
function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
}
function complete(tx: IDBTransaction) {
  return new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onabort = tx.onerror = () => reject(tx.error ?? new Error("transactionFailed")); });
}
function database() {
  if (!connection) connection = new Promise<IDBDatabase>((resolve, reject) => {
    if (typeof indexedDB === "undefined") { reject(new Error("unavailable")); return; }
    const open = indexedDB.open(PAGE_DRAFT_DB, 1);
    open.onupgradeneeded = () => { open.result.createObjectStore("drafts", { keyPath: "key" }); open.result.createObjectStore("meta"); };
    open.onsuccess = () => { open.result.onversionchange = () => { open.result.close(); connection = null; }; resolve(open.result); };
    open.onerror = () => reject(open.error);
    open.onblocked = () => reject(new Error("blocked"));
  }).catch((error) => { connection = null; throw error; });
  return connection;
}
function removeLegacy() {
  try { for (const key of Object.keys(sessionStorage)) if (key.startsWith(PAGE_DRAFT_PREFIX)) sessionStorage.removeItem(key); } catch { /* Never read legacy automatic snapshots. */ }
}
export function draftError(error: unknown, locale = "zh-CN") {
  return draftMessage(draftErrorCode(error), locale);
}
export async function clearPageDrafts() {
  generation++; removeLegacy(); announce({ type: "clear" });
  try {
    const db = await database(); const tx = db.transaction(["drafts", "meta"], "readwrite"); const done = complete(tx);
    tx.objectStore("drafts").clear(); tx.objectStore("meta").put(crypto.randomUUID(), "epoch"); await done;
  } catch { /* Unavailable persistence cannot prevent logout; ownership still prevents disclosure. */ }
}
export async function removePageDraft(owner: string, scope: string, expectedRevision?: string | null) {
  const db = await database(); const tx = db.transaction(["drafts", "meta"], "readwrite"); const done = complete(tx);
  const epoch = await request(tx.objectStore("meta").get("epoch")) ?? "initial";
  const key = draftKey(owner, scope);
  if (expectedRevision !== undefined) {
    const existing = await request<DraftRecord | undefined>(tx.objectStore("drafts").get(key));
    // A different tab may have saved newer, unsubmitted work during submission.
    if ((existing?.revision ?? null) !== expectedRevision) { await done; return; }
  }
  // Tombstones invalidate writers already running in any tab.
  tx.objectStore("drafts").put({ key, version: 2, owner, scope, epoch, revision: crypto.randomUUID(), deleted: true, savedAt: Date.now(), expires: Date.now() + PAGE_DRAFT_TTL, bytes: 0, files: [], data: null, businessVersion: "" } satisfies DraftRecord);
  await done; announce({ type: "write", key });
}
export async function readPageDraft<T>(owner: string, scope: string, codec: DraftCodec<T>): Promise<DraftLoad<T>> {
  removeLegacy();
  const db = await database(); const tx = db.transaction(["drafts", "meta"], "readwrite"); const done = complete(tx);
  const rows = await request<DraftRecord[]>(tx.objectStore("drafts").getAll());
  const epoch = await request(tx.objectStore("meta").get("epoch")) ?? "initial";
  const key = draftKey(owner, scope); let row = rows.find((item) => item.key === key) ?? null; let notice: string | null = null;
  for (const item of rows) {
    if (item.owner !== owner || item.expires <= Date.now() || item.version !== 2 || item.epoch !== epoch) {
      tx.objectStore("drafts").delete(item.key);
      if (item.key === key) { row = null; notice = "expired"; }
    }
  }
  await done;
  if (!row || row.deleted) return { value: null, record: row, epoch, notice };
  try {
    if (!Number.isFinite(row.savedAt) || row.expires > row.savedAt + PAGE_DRAFT_TTL || (!Number.isFinite(row.bytes) || row.bytes < 0 || row.bytes > MAX_DRAFT_BYTES) || !Array.isArray(row.files)) throw new Error();
    const value = codec.decode(row.data);
    if (!value || typeof value !== "object") throw new Error();
    for (const file of row.files) {
      if (Object.prototype.toString.call(file.bytes) !== "[object ArrayBuffer]" || typeof file.type !== "string" || typeof file.name !== "string" || !Number.isFinite(file.modified) || !Array.isArray(file.path) || !file.path.length || file.path.some((part) => typeof part !== "string" || ["__proto__", "prototype", "constructor"].includes(part))) throw new Error();
      if (file.checksum !== fileChecksum(file.bytes!)) throw new Error();
      let target = value as Record<string, unknown>;
      for (const part of file.path.slice(0, -1)) { if (!target[part] || typeof target[part] !== "object") throw new Error(); target = target[part] as Record<string, unknown>; }
      target[file.path.at(-1)!] = new File([file.bytes!], file.name, { type: file.type, lastModified: file.modified });
    }
    if (row.files.reduce((total, file) => total + (file.bytes?.byteLength ?? 0), 0) > MAX_DRAFT_BYTES) throw new Error();
    return { value, record: row, epoch, notice: "restored" };
  } catch {
    await removePageDraft(owner, scope);
    return { ...await readPageDraft(owner, scope, codec), notice: "corrupt" };
  }
}
export function preparePageDraft<T>(owner: string, scope: string, businessVersion: string, value: T, codec: DraftCodec<T>, epoch: string, expectedRevision: string | null): DraftWrite {
  const files: DraftRecord["files"] = [];
  function visit(item: unknown, path: string[]) {
    if (item instanceof Blob) files.push({ path, blob: item, name: item instanceof File ? item.name : "file", modified: item instanceof File ? item.lastModified : 0 });
    else if (item && typeof item === "object") Object.entries(item).forEach(([key, child]) => visit(child, [...path, key]));
  }
  visit(value, []);
  const data = codec.encode(value); const bytes = new Blob([JSON.stringify(data)]).size + files.reduce((sum, file) => sum + (file.blob?.size ?? 0), 0);
  if (bytes > MAX_DRAFT_BYTES) throw new Error("tooLarge");
  const savedAt = Date.now();
  return { generation, expectedRevision, record: { key: draftKey(owner, scope), version: 2, owner, scope, businessVersion, revision: crypto.randomUUID(), epoch, savedAt, expires: savedAt + PAGE_DRAFT_TTL, bytes, data, files } };
}
export async function writePageDrafts(writes: DraftWrite[]) {
  if (writes.reduce((sum, write) => sum + write.record.bytes, 0) > MAX_DRAFT_BYTES) throw new Error("tooLarge");
  // Read native files before the transaction. Store actual ArrayBuffer bytes:
  // ephemeral WebKit contexts can abort IndexedDB transactions containing Blobs.
  for (const write of writes) write.record.files = await Promise.all(write.record.files.map(async ({ blob, ...file }) => {
    if (!(blob instanceof Blob)) throw new Error("unreadableFile");
    const bytes = await blob.arrayBuffer();
    return { ...file, bytes, checksum: fileChecksum(bytes), type: blob.type };
  }));

  const db = await database(); const tx = db.transaction(["drafts", "meta"], "readwrite"); const done = complete(tx);
  try {
    const epoch = await request(tx.objectStore("meta").get("epoch")) ?? "initial";
    const rows = await request<DraftRecord[]>(tx.objectStore("drafts").getAll());
    for (const write of writes) {
      const existing = rows.find((item) => item.key === write.record.key);
      if (write.generation !== generation || write.record.epoch !== epoch) throw new Error("sessionChanged");
      if ((existing?.revision ?? null) !== write.expectedRevision) throw new Error("staleWrite");
    }
    const keys = new Set(writes.map((write) => write.record.key));
    const retained = rows.filter((row) => !keys.has(row.key) && !row.deleted && row.expires > Date.now());
    if (retained.length + writes.length > MAX_DRAFTS || retained.reduce((sum, row) => sum + row.bytes, 0) + writes.reduce((sum, write) => sum + write.record.bytes, 0) > MAX_TOTAL_BYTES) throw new Error("limit");
    for (const write of writes) tx.objectStore("drafts").put(write.record);
    await done; writes.forEach((write) => announce({ type: "write", key: write.record.key }));
  } catch (error) { try { tx.abort(); } catch { /* Already aborted. */ } await done.catch(() => {}); throw error; }
}
const fileIds = new WeakMap<Blob, number>(); let fileId = 0;
export function draftFingerprint(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => {
    if (!(item instanceof Blob)) return item;
    if (!fileIds.has(item)) fileIds.set(item, ++fileId);
    return { localFile: fileIds.get(item), size: item.size, type: item.type };
  });
}
/** Only pass explicitly selected, non-secret business fields. */
export function objectDraftCodec<T extends object>(shape?: T): DraftCodec<T> {
  function valid(value: unknown, sample: unknown): boolean {
    if (sample instanceof Blob) return value === null;
    if (sample === null) return value === null || typeof value === "object";
    if (typeof sample !== "object") return typeof value === typeof sample;
    if (Array.isArray(sample)) return Array.isArray(value) && (sample.length === 0 || value.every((item) => valid(item, sample[0])));
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    return Object.entries(sample as object).every(([key, item]) => key in value && valid((value as Record<string, unknown>)[key], item));
  }
  return { encode: (value) => JSON.parse(JSON.stringify(value, (_key, item: unknown) => item instanceof Blob ? null : item)), decode: (value) => value && typeof value === "object" && !Array.isArray(value) && (!shape || valid(value, shape)) ? value as T : null };
}

export async function listPageDrafts(owner: string): Promise<Array<Pick<DraftRecord, "scope" | "savedAt" | "expires" | "bytes"> & { title?: string }>> {
  const db = await database(); const tx = db.transaction(["drafts", "meta"], "readonly"); const done = complete(tx);
  const rows = await request<DraftRecord[]>(tx.objectStore("drafts").getAll());
  const epoch = await request(tx.objectStore("meta").get("epoch")) ?? "initial"; await done;
  return rows.filter((row) => row.owner === owner && row.epoch === epoch && row.version === 2 && !row.deleted && row.expires > Date.now()).map(({ scope, savedAt, expires, bytes, data }) => {
    const kind = scope.split(":")[0];
    const fields = data && typeof data === "object" ? data as Record<string, unknown> : {};
    const label = ["new-task", "metadata", "library-group"].includes(kind) ? fields.name : kind === "library-material" ? fields.filename : undefined;
    return { scope, savedAt, expires, bytes, ...(typeof label === "string" && label.trim() ? { title: label.trim().slice(0, 512) } : {}) };
  });
}
