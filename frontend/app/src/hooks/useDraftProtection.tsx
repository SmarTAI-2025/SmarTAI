import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { draftError, draftFingerprint, draftGeneration, draftKey, objectDraftCodec, preparePageDraft, readPageDraft, removePageDraft, subscribeDraftChanges, type DraftCodec, type DraftLoad, type DraftWrite } from "@/lib/pageDraftStore";
import { useDraftLeave, type DraftController } from "@/hooks/useDraftLeave";

export function useDraftOwner() { return useContext(DraftOwner); }
const DraftOwner = createContext<string | null>(null);
export function PageDraftSession({ ownerId, children }: { ownerId: string; children: ReactNode }) {
  return <DraftOwner.Provider key={ownerId} value={ownerId}>{children}</DraftOwner.Provider>;
}

type Options<T extends object> = {
  scope: string; value: T; onRestore: (value: T) => void; baseline?: T; version?: string;
  enabled?: boolean; busy?: boolean; secret?: boolean; codec?: DraftCodec<T>;
};
/** Adapts existing business editors without changing their submit/confirmation contract. */
export function useDraftProtection<T extends object>({ scope, value, onRestore, baseline, version = "", enabled = true, busy = false, secret = false, codec }: Options<T>) {
  const owner = useContext(DraftOwner);
  const leave = useDraftLeave();
  const [formalPending, setFormalPending] = useState(false);
  const formalActive = useRef(false);
  const [loaded, setLoaded] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState<T | null>(null);
  const [, render] = useState(0);
  const latest = useRef({ value, onRestore, baseline, version, busy, enabled });
  latest.current = { value, onRestore, baseline, version, busy, enabled };
  const saved = useRef<string | null>(null);
  const initialValue = useRef(baseline ?? value);
  const initial = useRef(draftFingerprint(initialValue.current));
  const cleared = useRef<string | null>(null);
  const load = useRef<DraftLoad<T> | null>(null);
  const disposed = useRef(false);
  const incarnation = useRef(0);
  const lastVersion = useRef(version);
  const epoch = useRef(draftGeneration());
  const codecRef = useRef(codec ?? objectDraftCodec<T>(value));
  const fingerprint = draftFingerprint(value);
  const baselineFingerprint = draftFingerprint(baseline ?? value);
  const completedBusy = useRef(false);
  const effectiveBusy = busy || formalPending;
  const previousBusy = useRef(effectiveBusy);
  if (effectiveBusy && !previousBusy.current) completedBusy.current = false;
  previousBusy.current = effectiveBusy;
  const isDirty = () => {
    const now = draftFingerprint(latest.current.value);
    const base = latest.current.baseline ? draftFingerprint(latest.current.baseline) : initial.current;
    // A successful business save may render the old query cache briefly.
    if (cleared.current && (now === cleared.current || now === base)) return false;
    return now !== (saved.current ?? base);
  };
  const dirty = enabled && isDirty();
  const state = useRef({ dirty, loaded, savedAt, notice, conflict });
  state.current = { dirty, loaded, savedAt, notice, conflict };
  const id = useRef(Symbol(scope));
  const identity = useRef({ owner, scope }); identity.current = { owner, scope };
  const isCurrent = () => !disposed.current && (secret || epoch.current === draftGeneration()) && identity.current.owner === owner && identity.current.scope === scope;

  useEffect(() => {
    if (!enabled) return;
    disposed.current = false; let cancelled = false;
    if (!state.current.loaded) lastVersion.current = latest.current.version;
    codecRef.current = codec ?? objectDraftCodec<T>(latest.current.value);
    const startValue = draftFingerprint(latest.current.value);
    if (secret || !owner) { setLoaded(true); return; }
    readPageDraft(owner, scope, codecRef.current).then((result) => {
      if (cancelled || epoch.current !== draftGeneration()) return;
      load.current = result; setLoaded(true); setNotice(result.notice);
      if (!result.value || !result.record) return;
      const currentFingerprint = draftFingerprint(latest.current.value);
      const hydratedBaseline = latest.current.baseline && draftFingerprint(latest.current.baseline);
      if (result.record.businessVersion !== latest.current.version || (currentFingerprint !== startValue && currentFingerprint !== hydratedBaseline)) {
        setConflict(result.value); setNotice("发现已暂存草稿，但服务器内容或当前输入已变化。请核对后明确恢复，未自动覆盖。"); return;
      }
      saved.current = draftFingerprint(result.value); setSavedAt(result.record.savedAt);
      latest.current.onRestore(result.value);
    }).catch((error) => { if (!cancelled) { setLoaded(true); setNotice(draftError(error)); } });
    return () => { cancelled = true; disposed.current = true; };
    // Business version changes are conflicts, not a new editor identity.
  }, [enabled, owner, scope, secret]);

  useEffect(() => {
    if (cleared.current) { saved.current = null; cleared.current = null; render((n) => n + 1); }
  }, [baselineFingerprint]);
  useEffect(() => {
    if (!enabled) { lastVersion.current = version; return; }
    if (lastVersion.current === version) return;
    lastVersion.current = version;
    if (cleared.current) return;
    if (load.current?.value || isDirty()) {
      setConflict(load.current?.value ?? latest.current.value);
      setNotice("服务器业务版本已变化。当前输入与已暂存草稿保留，请核对后恢复或删除；未覆盖正式结果。");
    } else if (latest.current.baseline) latest.current.onRestore(latest.current.baseline);
  }, [version, enabled]);

  useEffect(() => subscribeDraftChanges((event) => {
    const detail = (event as CustomEvent<{ type: string; key?: string; remote?: boolean }>).detail;
    if (detail.remote && detail.type === "write" && owner && detail.key === draftKey(owner, scope)) {
      setNotice("另一标签页已暂存或删除此草稿。当前输入保留；再次暂存不会覆盖它，请重新打开页面核对最新版本。");
    }
    if (detail.type === "clear") {
      saved.current = null; load.current = null; setSavedAt(null); setConflict(null);
      latest.current.onRestore(latest.current.baseline ?? initialValue.current);
      setNotice("本机草稿已清除。退出、切换账号或会话失效后不会恢复上个账号的输入。");
    }
  }), [owner, scope]);

  async function clear(snapshot?: T) {
    if (!isCurrent()) return false;
    incarnation.current += 1;
    completedBusy.current = true;
    const submitted = draftFingerprint(snapshot ?? latest.current.value);
    const unchanged = submitted === draftFingerprint(latest.current.value);
    saved.current = submitted; cleared.current = submitted; setSavedAt(null); setConflict(null);
    if (load.current) load.current = { ...load.current, value: null };
    if (owner) {
      try { await removePageDraft(owner, scope); load.current = await readPageDraft(owner, scope, codecRef.current); }
      catch { setNotice("正式操作已完成，但浏览器草稿清理失败；旧草稿恢复前仍会核对业务版本。"); }
    }
    return unchanged;
  }
  async function runFormal(submit: () => Promise<unknown>, snapshot: T) {
    if (formalActive.current || !isCurrent()) return false;
    formalActive.current = true; completedBusy.current = false; setFormalPending(true);
    try {
      await submit();
      if (!isCurrent()) return false;
      const unchanged = await clear(snapshot);
      return isCurrent() && unchanged;
    } finally { formalActive.current = false; setFormalPending(false); }
  }
  function discardEdits() {
    const previous = load.current?.value ?? latest.current.baseline;
    if (previous) latest.current.onRestore(previous);
    else latest.current.onRestore(initialValue.current);
    // A server conflict's saved draft must not become an unannounced restore.
    if (conflict) latest.current.onRestore(latest.current.baseline ?? initialValue.current);
    saved.current = draftFingerprint(previous ?? latest.current.baseline ?? initialValue.current);
    render((n) => n + 1);
  }
  function restoreConflict() {
    if (!conflict) return;
    latest.current.onRestore(conflict); saved.current = null; setConflict(null); setSavedAt(null);
    setNotice("已恢复旧草稿供核对，尚未正式保存。请检查业务内容后重新暂存或提交。");
  }
  function resetWorking(value: T) {
    incarnation.current += 1;
    if (load.current) load.current = { ...load.current, value: null };
    saved.current = draftFingerprint(value); cleared.current = null; setSavedAt(null); setConflict(null);
    latest.current.onRestore(value);
    setNotice("工作区已载入最新内容；之前明确暂存的版本仍保留，重新打开时会核对服务器版本。");
  }
  const controller = useRef<DraftController>(null!);
  controller.current = {
    id: id.current, scope, secret, get active() { return latest.current.enabled; },
    get dirty() { return (secret || epoch.current === draftGeneration()) && latest.current.enabled && isDirty(); }, get busy() { return (secret || epoch.current === draftGeneration()) && (formalActive.current || (latest.current.busy && !completedBusy.current)); },
    get loaded() { return state.current.loaded; }, get savedAt() { return state.current.savedAt; },
    get notice() { return state.current.notice; }, get hasConflict() { return Boolean(state.current.conflict); },
    discard: discardEdits, restore: restoreConflict,
    remove: async () => {
      if (!owner || disposed.current || epoch.current !== draftGeneration()) return;
      await removePageDraft(owner, scope);
      incarnation.current += 1;
      load.current = await readPageDraft(owner, scope, codecRef.current);
      saved.current = null; cleared.current = null; setSavedAt(null); setConflict(null);
      latest.current.onRestore(latest.current.baseline ?? initialValue.current);
      setNotice("已删除本页本地草稿。");
    },
    prepare: () => {
      cleared.current = null;
      if (secret || !owner) throw new Error("认证秘密仅保留在当前表单，不能作为普通草稿暂存。请使用原有保存操作或不暂存离开。");
      if (!state.current.loaded) throw new Error("正在读取本地草稿，请稍后再暂存。");
      if (epoch.current !== draftGeneration() || disposed.current) throw new Error("此编辑页面已失效，本次未暂存。");
      if (state.current.conflict) throw new Error("请先核对服务器变化，明确恢复或删除旧草稿后再暂存。");
      const snapshot = latest.current.value; const snapshotFingerprint = draftFingerprint(snapshot);
      const capturedIncarnation = incarnation.current;
      const write = preparePageDraft(owner, scope, latest.current.version, snapshot, codecRef.current, load.current?.epoch ?? "initial", load.current?.record?.revision ?? null);
      return { write, commit: () => {
        if (disposed.current || capturedIncarnation !== incarnation.current || epoch.current !== draftGeneration()) return;
        load.current = { value: snapshot, record: write.record, epoch: write.record.epoch, notice: null };
        saved.current = snapshotFingerprint; setSavedAt(write.record.savedAt); setNotice("草稿已保存。");
      } };
    },
  };
  useEffect(() => leave.register(() => controller.current), [leave.register]);
  useEffect(() => { leave.changed(); }, [fingerprint, dirty, loaded, busy, formalPending, savedAt, notice, conflict, leave.changed]);
  return { dirty, loaded, savedAt, notice, conflict, isCurrent, clear, runFormal, formalPending, resetWorking, discardEdits, restoreConflict, requestLeave: (run: () => void) => leave.request(run, [id.current]), controller: () => controller.current };
}
export type PreparedDraft = { write: DraftWrite; commit: () => void };
