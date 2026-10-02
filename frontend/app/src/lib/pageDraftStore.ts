/** Tab-scoped, bounded drafts. Files stay in memory; codecs only persist form metadata. */
export const PAGE_DRAFT_PREFIX = "smartai:page-draft:";
export const PAGE_DRAFT_TTL = 8 * 60 * 60 * 1000;
const MAX_BYTES = 96_000;
const MAX_DRAFTS = 20;
type Envelope = { version: 1; owner: string; scope: string; expires: number; data: unknown };
type Entry = Envelope & { value: unknown; persisted: boolean };
const memory = new Map<string, Entry>();
let generation = 0;
let activeOwner: string | null = null;

export interface DraftCodec<T> {
  encode(value: T): unknown;
  decode(value: unknown): T | null;
}

export function draftKey(owner: string, scope: string) {
  return PAGE_DRAFT_PREFIX + JSON.stringify([owner, scope]);
}

function storage() { return window.sessionStorage; }

export function clearPageDrafts() {
  generation += 1;
  activeOwner = null;
  memory.clear();
  try {
    for (const key of Object.keys(storage())) {
      if (key.startsWith(PAGE_DRAFT_PREFIX)) storage().removeItem(key);
    }
  } catch { /* Unavailable storage must not prevent logout. */ }
}

export function draftGeneration() { return generation; }

export function removePageDraft(owner: string, scope: string) {
  const key = draftKey(owner, scope);
  memory.delete(key);
  try { storage().removeItem(key); } catch { /* Memory-only fallback. */ }
}

export function readPageDraft<T>(owner: string, scope: string, codec: DraftCodec<T>): {
  value: T | null; notice: "restored" | "discarded" | "memory" | null;
} {
  if (activeOwner && activeOwner !== owner) clearPageDrafts();
  activeOwner = owner;
  const key = draftKey(owner, scope);
  const cached = memory.get(key);
  if (cached && cached.expires > Date.now()) return { value: cached.value as T, notice: cached.persisted ? "restored" : "memory" };
  memory.delete(key);
  try {
    // Shared devices: evict other owners, expired records, and incompatible schemas.
    for (const item of Object.keys(storage()).filter((item) => item.startsWith(PAGE_DRAFT_PREFIX))) {
      if (item === key) continue;
      try {
        const entry = JSON.parse(storage().getItem(item) ?? "null") as Envelope | null;
        if (!entry || entry.owner !== owner || entry.version !== 1 || !(entry.expires > Date.now())) storage().removeItem(item);
      } catch { storage().removeItem(item); }
    }
    const raw = storage().getItem(key);
    if (!raw) return { value: null, notice: null };
    const entry = raw.length <= MAX_BYTES ? JSON.parse(raw) as Envelope : null;
    const value = entry?.version === 1 && entry.owner === owner && entry.scope === scope
      && entry.expires > Date.now() && entry.expires <= Date.now() + PAGE_DRAFT_TTL
      ? codec.decode(entry.data) : null;
    if (!value || !entry) {
      removePageDraft(owner, scope);
      return { value: null, notice: "discarded" };
    }
    memory.set(key, { ...entry, value, persisted: true });
    return { value, notice: "restored" };
  } catch (error) {
    removePageDraft(owner, scope);
    return { value: null, notice: error instanceof SyntaxError ? "discarded" : "memory" };
  }
}

export function writePageDraft<T>(owner: string, scope: string, value: T, codec: DraftCodec<T>): boolean {
  const key = draftKey(owner, scope);
  const entry: Envelope = { version: 1, owner, scope, expires: Date.now() + PAGE_DRAFT_TTL, data: null };
  memory.delete(key);
  memory.set(key, { ...entry, value, persisted: false });
  for (const [id, cached] of memory) if (cached.expires <= Date.now()) memory.delete(id);
  while (memory.size > MAX_DRAFTS) memory.delete(memory.keys().next().value!);
  try {
    entry.data = codec.encode(value);
    const json = JSON.stringify(entry);
    if (json.length > MAX_BYTES) throw new Error("draft_too_large");
    storage().setItem(key, json);
    memory.set(key, { ...entry, value, persisted: true });
    const keys = Object.keys(storage()).filter((item) => item.startsWith(PAGE_DRAFT_PREFIX));
    if (keys.length > MAX_DRAFTS) {
      const oldest = keys.filter((item) => item !== key).sort((a, b) => {
        try { return JSON.parse(storage().getItem(a)!).expires - JSON.parse(storage().getItem(b)!).expires; }
        catch { return 0; }
      });
      oldest.slice(0, keys.length - MAX_DRAFTS).forEach((item) => storage().removeItem(item));
    }
    return true;
  } catch {
    // Never restore a previous successful snapshot after a failed save.
    try { storage().removeItem(key); } catch { /* Storage is blocked. */ }
    return false;
  }
}
