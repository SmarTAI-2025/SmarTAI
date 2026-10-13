// Guest credentials are tab-local and never replace the signed-in teacher.
const TOKEN_KEY = "smartai_frontier_token";
const TASKS_KEY = "smartai_frontier_tasks";
const SESSION_EVENT = "smartai-frontier-session";
const GENERATION_KEY = "smartai_frontier_generation";
const OWNER_KEY = "smartai_frontier_owner";
const retiredTasks = new Map<string, number>();

export function getFrontierGeneration(): number {
  return typeof sessionStorage === "undefined" ? 0 : Number(sessionStorage.getItem(GENERATION_KEY) ?? 0);
}

export function beginFrontierSession(token: string, ownerId: string): void {
  if (sessionStorage.getItem(OWNER_KEY) !== ownerId) {
    retireFrontierTasks();
    sessionStorage.setItem(GENERATION_KEY, String(getFrontierGeneration() + 1));
    sessionStorage.setItem(OWNER_KEY, ownerId);
  }
  setFrontierToken(token);
}

function retireFrontierTasks(): void {
  for (const taskId of frontierTasks()) retiredTasks.set(taskId, getFrontierGeneration());
  sessionStorage.removeItem(TASKS_KEY);
}

export function endFrontierSessionLocally(): void {
  retireFrontierTasks();
  clearFrontierToken();
}

export function getFrontierToken(): string | null {
  return typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(TOKEN_KEY);
}

export function setFrontierToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
  window.dispatchEvent(new Event(SESSION_EVENT));
}

export function clearFrontierToken(): void {
  sessionStorage.setItem(GENERATION_KEY, String(getFrontierGeneration() + 1));
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(OWNER_KEY);
  window.dispatchEvent(new Event(SESSION_EVENT));
}

export function subscribeFrontierSession(listener: () => void) {
  window.addEventListener(SESSION_EVENT, listener);
  return () => window.removeEventListener(SESSION_EVENT, listener);
}

function frontierTasks(): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(TASKS_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
  } catch { return []; }
}

export function rememberFrontierTask(taskId: string): void {
  retiredTasks.delete(taskId);
  sessionStorage.setItem(TASKS_KEY, JSON.stringify([...new Set([...frontierTasks(), taskId])]));
}

export function isFrontierTaskPath(path: string): boolean {
  const taskId = path.match(/^\/(?:tasks|analytics)\/([^/?#]+)/)?.[1];
  try { return Boolean(taskId && frontierTasks().includes(decodeURIComponent(taskId))); }
  catch { return false; }
}

// Keep only in-memory tombstones after leaving: an asynchronous Demo workflow
// cannot send its next request with the still-valid teacher login.
export function frontierTaskRequestGeneration(path: string): number | null {
  const match = path.match(/^\/(?:tasks|analytics)\/([^/?#]+)/)?.[1];
  if (!match) return null;
  try {
    const id = decodeURIComponent(match);
    return frontierTasks().includes(id) ? getFrontierGeneration() : retiredTasks.get(id) ?? null;
  } catch { return null; }
}

export function isFrontierWorkspacePath(path: string): boolean {
  return path === "/frontier/live" || isFrontierTaskPath(path);
}

export function taskWorkspacePath(taskId?: string, teacherPath = "/dashboard"): string {
  return taskId && frontierTasks().includes(taskId) ? `/frontier/live?taskId=${encodeURIComponent(taskId)}` : teacherPath;
}
