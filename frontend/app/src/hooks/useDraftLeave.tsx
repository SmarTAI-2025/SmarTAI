import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { UNSAFE_DataRouterContext, useBlocker } from "react-router-dom";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { draftError, subscribeDraftChanges, writePageDrafts } from "@/lib/pageDraftStore";
import type { PreparedDraft } from "@/hooks/useDraftProtection";
import { isSessionExpired, useSessionExpired } from "@/lib/sessionExpiry";
export interface DraftController {
  id: symbol; scope: string; secret: boolean; active: boolean; dirty: boolean; busy: boolean; loaded: boolean;
  savedAt: number | null; notice: string | null; hasConflict: boolean;
  prepare: () => PreparedDraft; discard: () => void; remove: () => Promise<void>; restore: () => void; keepCurrent: () => void;
}
type Getter = () => DraftController;
type Intent = { run: () => void; cancel: () => void; controllers: DraftController[] };
const noop = () => {};
const LeaveContext = createContext({ register: (_getter: Getter): (() => void) => noop, changed: noop, request: (run: () => void, _ids?: symbol[]) => run(), controllers: [] as DraftController[], save: async (_ids?: symbol[]) => {}, saving: false, error: null as string | null });
export function useDraftLeave() { return useContext(LeaveContext); }
export function DraftLeaveProvider({ children }: { children: ReactNode }) {
  const expired = useSessionExpired();
  const registry = useRef(new Map<symbol, Getter>());
  const [, render] = useState(0);
  const [intent, setIntent] = useState<Intent | null>(null);
  const pending = useRef<Intent | null>(null);
  const savingRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const changed = useCallback(() => render((value) => value + 1), []);
  const all = useCallback(() => [...registry.current.values()].map((get) => get()).filter((item) => item.active), []);
  const register = useCallback((get: Getter) => {
    const id = get().id; registry.current.set(id, get); changed();
    return () => { registry.current.delete(id); changed(); };
  }, [changed]);
  const ask = useCallback((run: () => void, ids?: symbol[], cancel = noop) => {
    if (isSessionExpired()) { run(); return; }
    if (pending.current || savingRef.current) return;
    const controllers = all().filter((controller) => (!ids || ids.includes(controller.id)) && (controller.dirty || controller.busy));
    if (!controllers.length) { run(); return; }
    const next = { run, cancel, controllers }; pending.current = next; setError(null); setIntent(next);
  }, [all]);
  const save = useCallback(async (ids?: symbol[]) => {
    if (savingRef.current) return;
    savingRef.current = true; setSaving(true); setError(null);
    try {
      const controllers = all().filter((item) => !item.secret && (!ids || ids.includes(item.id)) && (Boolean(ids) || item.dirty || item.savedAt !== null));
      if (controllers.some((item) => item.busy)) throw new Error("业务保存正在进行，请等完成后再暂存或离开。");
      const prepared = controllers.map((item) => item.prepare());
      await writePageDrafts(prepared.map((item) => item.write));
      prepared.forEach((item) => item.commit());
    } catch (failure) { setError(draftError(failure)); throw failure; }
    finally { savingRef.current = false; setSaving(false); }
  }, [all]);
  const controllers = all();
  useEffect(() => {
    const original = pending.current;
    if (!original || savingRef.current || error) return;
    const current = all();
    if (original.controllers.every(item => current.some(now => now.id === item.id && !now.dirty && !now.busy))) {
      pending.current = null; setIntent(null); original.run();
    }
  }, [controllers, error, all]);
  const dirty = controllers.some((item) => item.dirty || item.busy);
  useEffect(() => {
    if (!dirty || expired) return;
    function protect(event: BeforeUnloadEvent) {
      // A durable write can finish before React removes this listener.
      if (isSessionExpired() || !all().some((item) => item.dirty || item.busy)) return;
      event.preventDefault(); event.returnValue = "";
    }
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, expired, all]);
  function finish(leave: boolean) {
    const original = pending.current; if (!original || savingRef.current) return;
    pending.current = null; setIntent(null); setError(null);
    if (leave) { original.controllers.forEach((item) => item.discard()); original.run(); }
    else original.cancel();
  }
  async function saveAndLeave() {
    const original = pending.current; if (!original || savingRef.current) return;
    try {
      const registeredIds = all().map((item) => item.id);
      if (original.controllers.some((item) => !registeredIds.includes(item.id))) throw new Error("编辑对象已变化，原离开操作已取消。请核对当前页面。");
      await save(original.controllers.map((item) => item.id));
      // Set-state commits are asynchronous; defer the check to the next render boundary.
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
      if (pending.current !== original) return;
      if (original.controllers.some((item) => !all().some((registered) => registered.id === item.id))) { setError("编辑对象已变化，请核对当前页面；原离开操作未继续。"); return; }
      if (all().filter((item) => original.controllers.some((originalItem) => originalItem.id === item.id)).some((item) => item.dirty || item.busy)) { setError("暂存期间内容又有修改，已保留输入；请再次暂存后离开。"); return; }
      pending.current = null; setIntent(null); original.run();
    } catch { /* Preserve the original intent and all input for retry. */ }
  }
  useEffect(() => subscribeDraftChanges((event) => {
    if ((event as CustomEvent<{ type: string }>).detail.type !== "clear") return;
    const original = pending.current; pending.current = null; setIntent(null); original?.cancel();
  }), []);
  const routeAsk = useCallback((run: () => void, cancel?: () => void) => ask(run, undefined, cancel), [ask]);
  const dataRouter = useContext(UNSAFE_DataRouterContext);
  return <LeaveContext.Provider value={{ register, changed, request: ask, controllers, save, saving, error }}>
    {dataRouter ? <RouterLeaveGuard shouldBlock={() => !isSessionExpired() && all().some((item) => item.dirty || item.busy)} ask={routeAsk} /> : null}
    {children}
    {intent && !expired ? <UnsavedChangesDialog title="有未暂存修改" description={controllers.some((item) => item.busy) ? "业务保存正在进行，请等待完成；当前输入仍保留。" : intent.controllers.some((item) => item.secret) ? "认证秘密不会写入本地草稿。请继续编辑并使用原有保存操作，或放弃本次输入离开。" : "暂存仅保存在当前浏览器，不会上传、识别、批改或确认复核。"} stayLabel="继续编辑" leaveLabel="不暂存并离开" saveLabel={intent.controllers.some((item) => item.secret) ? undefined : "暂存并离开"} savingLabel="正在暂存…" saving={saving || controllers.some((item) => item.busy)} saveError={error ?? undefined} onStay={() => finish(false)} onLeave={() => finish(true)} onSave={() => void saveAndLeave()} /> : null}
  </LeaveContext.Provider>;
}
function RouterLeaveGuard({ shouldBlock, ask }: { shouldBlock: () => boolean; ask: (run: () => void, cancel?: () => void) => void }) {
  const blocker = useBlocker(() => shouldBlock());
  const dataRouter = useContext(UNSAFE_DataRouterContext);
  useEffect(() => {
    if (blocker.state !== "blocked" || !dataRouter) return;
    const router = dataRouter.router;
    const entry = [...router.state.blockers.entries()].find(([, current]) => current === blocker);
    // Router state updates synchronously; a render/effect may still hold the
    // previous blocked object after another navigation has already cleared it.
    if (!entry) return;
    const [key] = entry;
    const stillBlocked = () => router.state.blockers.get(key)?.state === "blocked";
    ask(
      () => { if (stillBlocked()) blocker.proceed(); },
      () => { if (stillBlocked()) blocker.reset(); },
    );
  }, [blocker, dataRouter, ask]);
  return null;
}
export function DraftActions() {
  const { controllers, save, saving, error } = useDraftLeave();
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const editable = controllers.filter((item) => !item.secret);
  if (!editable.length) return null;
  const dirty = editable.some((item) => item.dirty); const busy = saving || editable.some((item) => item.busy || !item.loaded);
  const savedAt = Math.max(...editable.map((item) => item.savedAt ?? 0));
  return <section aria-label="本地草稿" className="my-5 w-full rounded-[10px] border bg-card px-4 py-3 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p role="status">{dirty ? "有未暂存修改" : savedAt ? `已暂存 · ${new Date(savedAt).toLocaleString()}` : "尚未暂存"}</p>
      <div className="flex flex-wrap gap-2"><button type="button" disabled={busy} onClick={() => void save().catch(() => {})} className="rounded-md bg-primary px-4 py-2 font-semibold text-primary-foreground disabled:opacity-50">{saving ? "正在暂存…" : "暂存"}</button><button type="button" disabled={busy} onClick={() => { if (window.confirm("删除本页已暂存草稿并重置输入？正式业务结果不受影响。")) { setDeleteError(null); void Promise.all(editable.map((item) => item.remove())).catch((failure) => setDeleteError(draftError(failure))); } }} className="rounded-md border px-3 py-2">删除本页草稿</button></div>
    </div>
    <div className="mt-2 flex flex-wrap items-start gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <p>仅保存在此浏览器，7 天内可恢复。</p>
      <details><summary className="cursor-pointer">保存说明</summary><p className="mt-1 max-w-lg">退出登录或清理浏览器数据会清除草稿。每页最多 64 MiB，总计 128 MiB、30 份。</p></details>
    </div>
    {editable.map((item) => <div key={item.scope}>{item.notice ? <p role="status" className="mt-2 text-xs">{item.notice}</p> : null}{item.hasConflict ? <div className="mt-2 flex flex-wrap gap-2"><button type="button" onClick={item.keepCurrent} className="rounded border px-3 py-2 text-sm">保留当前输入</button><button type="button" onClick={item.restore} className="rounded border px-3 py-2 text-sm">核对后恢复旧草稿</button></div> : null}</div>)}
    {error ? <p role="alert" className="mt-2 text-danger">{error}</p> : null}
    {deleteError ? <p role="alert" className="mt-2 text-danger">{deleteError}</p> : null}
  </section>;
}
