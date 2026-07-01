export interface PersonalKBDoc {
  id: string;
  filename: string;
  size: number;
  source: "manual" | "task-upload";
  createdAt: number;
  taskId?: string;
  taskDocId?: string;
  chunkCount?: number;
  embedder?: string;
}

const DOCS_KEY = "smartai_personal_kb_docs";
const TASK_SELECTION_PREFIX = "smartai_task_personal_kb:";

export function listPersonalKBDocs(): PersonalKBDoc[] {
  return readDocs().sort((a, b) => b.createdAt - a.createdAt);
}

export function addPersonalKBDoc(input: {
  filename: string;
  size: number;
  source: PersonalKBDoc["source"];
  taskId?: string;
  taskDocId?: string;
  chunkCount?: number;
  embedder?: string;
}): PersonalKBDoc {
  const docs = readDocs();
  const existing = docs.find(
    (doc) => doc.filename === input.filename && doc.size === input.size && doc.source === input.source,
  );
  if (existing) {
    return existing;
  }

  const doc: PersonalKBDoc = {
    id: `pkb_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
    filename: input.filename,
    size: input.size,
    source: input.source,
    createdAt: Date.now(),
    taskId: input.taskId,
    taskDocId: input.taskDocId,
    chunkCount: input.chunkCount,
    embedder: input.embedder,
  };
  writeDocs([doc, ...docs]);
  return doc;
}

export function deletePersonalKBDoc(docId: string): void {
  writeDocs(readDocs().filter((doc) => doc.id !== docId));
  const taskIds = Object.keys(window.localStorage).filter((key) => key.startsWith(TASK_SELECTION_PREFIX));
  for (const key of taskIds) {
    const selected = readStringArray(key).filter((id) => id !== docId);
    window.localStorage.setItem(key, JSON.stringify(selected));
  }
}

export function getTaskPersonalKBSelection(taskId: string): string[] {
  return readStringArray(selectionKey(taskId));
}

export function setTaskPersonalKBSelection(taskId: string, docIds: string[]): void {
  window.localStorage.setItem(selectionKey(taskId), JSON.stringify([...new Set(docIds)]));
}

function selectionKey(taskId: string) {
  return `${TASK_SELECTION_PREFIX}${taskId}`;
}

function readDocs(): PersonalKBDoc[] {
  try {
    const raw = window.localStorage.getItem(DOCS_KEY);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(isPersonalKBDoc);
  } catch {
    return [];
  }
}

function writeDocs(docs: PersonalKBDoc[]) {
  window.localStorage.setItem(DOCS_KEY, JSON.stringify(docs));
}

function readStringArray(key: string): string[] {
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function isPersonalKBDoc(value: unknown): value is PersonalKBDoc {
  if (!value || typeof value !== "object") {
    return false;
  }
  const doc = value as Partial<PersonalKBDoc>;
  return (
    typeof doc.id === "string" &&
    typeof doc.filename === "string" &&
    typeof doc.size === "number" &&
    typeof doc.createdAt === "number" &&
    (doc.source === "manual" || doc.source === "task-upload")
  );
}
