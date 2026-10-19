import { draftErrorCode, draftMessage } from "@/lib/draftMessages";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { draftFingerprint, draftGeneration, draftKey, objectDraftCodec, preparePageDraft, readPageDraft, removePageDraft, subscribeDraftChanges, type DraftCodec, type DraftLoad, type DraftWrite } from "@/lib/pageDraftStore";
import { useDraftLeave, type DraftController } from "@/hooks/useDraftLeave";
import { toast } from "sonner";

export function useDraftOwner() { return useContext(DraftOwner); }
const DraftOwner = createContext<string | null>(null);
export function PageDraftSession({ ownerId, children }: { ownerId: string; children: ReactNode }) {
  return <DraftOwner.Provider key={ownerId} value={ownerId}>{children}</DraftOwner.Provider>;
}

type Options<T extends object> = {
  scope: string; value: T; onRestore: (value: T) => void; baseline?: T; version?: string;
  enabled?: boolean; busy?: boolean; secret?: boolean; codec?: DraftCodec<T>;
  versionsMatch?: (stored: string, current: string) => boolean;
};
/** Adapts existing business editors without changing their submit/confirmation contract. */
export function useDraftProtection<T extends object>({ scope, value, onRestore, baseline, version = "", enabled = true, busy = false, secret = false, codec, versionsMatch }: Options<T>) {
  const owner = useContext(DraftOwner);
  const leave = useDraftLeave();
  const locale = leave.locale;
  const currentLocale = useRef(locale); currentLocale.current = locale;
  const [formalPending, setFormalPending] = useState(false);
  const formalActive = useRef(false);
  const [loaded, setLoaded] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState<T | null>(null);
  const [, render] = useState(0);
  const latest = useRef({ value, onRestore, baseline, version, busy, enabled, versionsMatch });
  latest.current = { value, onRestore, baseline, version, busy, enabled, versionsMatch };
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
      const compatible = latest.current.versionsMatch?.(result.record.businessVersion, latest.current.version) ?? result.record.businessVersion === latest.current.version;
      if (!compatible || (currentFingerprint !== startValue && currentFingerprint !== hydratedBaseline)) {
        setConflict(result.value); setNotice("restoreConflict"); return;
      }
      saved.current = draftFingerprint(result.value); setSavedAt(result.record.savedAt);
      latest.current.onRestore(result.value);
    }).catch((error) => { if (!cancelled) { setLoaded(true); setNotice(draftErrorCode(error)); } });
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
      setNotice("versionConflict");
    } else if (latest.current.baseline) latest.current.onRestore(latest.current.baseline);
  }, [version, enabled]);

  useEffect(() => subscribeDraftChanges((event) => {
    const detail = (event as CustomEvent<{ type: string; key?: string; remote?: boolean }>).detail;
    if (detail.remote && detail.type === "write" && owner && detail.key === draftKey(owner, scope)) {
      setNotice("remoteChange");
    }
    if (detail.type === "clear") {
      saved.current = null; load.current = null; setSavedAt(null); setConflict(null);
      latest.current.onRestore(latest.current.baseline ?? initialValue.current);
      setNotice("cleared");
    }
  }), [owner, scope]);

  async function clear(snapshot?: T) {
    if (!isCurrent()) return false;
    const ownsLock = !formalActive.current;
    if (ownsLock) { formalActive.current = true; setFormalPending(true); }
    try {
      incarnation.current += 1;
      completedBusy.current = true;
      const submitted = draftFingerprint(snapshot ?? latest.current.value);
      saved.current = submitted; cleared.current = submitted; setSavedAt(null); setConflict(null);
      if (load.current) load.current = { ...load.current, value: null };
      if (owner) {
        try { await removePageDraft(owner, scope, load.current?.record?.revision ?? null); load.current = await readPageDraft(owner, scope, codecRef.current); }
        catch {
          const message = "cleanupFailed";
          setNotice(message);
          // The form may navigate immediately; keep this outcome visible there.
          toast.warning(draftMessage(message, currentLocale.current));
        }
      }
      return submitted === draftFingerprint(latest.current.value);
    } finally {
      if (ownsLock) { formalActive.current = false; setFormalPending(false); }
    }
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
    setNotice("conflictRestored");
  }
  function keepCurrent() {
    saved.current = null; setConflict(null); setSavedAt(null);
    setNotice("keptCurrent");
  }
  function resetWorking(value: T) {
    incarnation.current += 1;
    if (load.current) load.current = { ...load.current, value: null };
    saved.current = draftFingerprint(value); cleared.current = null; setSavedAt(null); setConflict(null);
    latest.current.onRestore(value);
    setNotice("latestLoaded");
  }
  const controller = useRef<DraftController>(null!);
  controller.current = {
    id: id.current, scope, secret, get active() { return latest.current.enabled; },
    get dirty() { return (secret || epoch.current === draftGeneration()) && latest.current.enabled && isDirty(); }, get busy() { return (secret || epoch.current === draftGeneration()) && (formalActive.current || (latest.current.busy && !completedBusy.current)); },
    get loaded() { return state.current.loaded; }, get savedAt() { return state.current.savedAt; },
    get notice() { return state.current.notice; }, get hasConflict() { return Boolean(state.current.conflict); },
    discard: discardEdits, restore: restoreConflict, keepCurrent,
    remove: async () => {
      if (!owner || disposed.current || epoch.current !== draftGeneration()) return;
      await removePageDraft(owner, scope);
      incarnation.current += 1;
      load.current = await readPageDraft(owner, scope, codecRef.current);
      saved.current = null; cleared.current = null; setSavedAt(null); setConflict(null);
      latest.current.onRestore(latest.current.baseline ?? initialValue.current);
      setNotice("deleted");
    },
    prepare: () => {
      cleared.current = null;
      if (secret || !owner) throw new Error("secret");
      if (!state.current.loaded) throw new Error("loading");
      if (epoch.current !== draftGeneration() || disposed.current) throw new Error("invalidEditor");
      if (state.current.conflict) throw new Error("unresolvedConflict");
      const snapshot = latest.current.value; const snapshotFingerprint = draftFingerprint(snapshot);
      const capturedIncarnation = incarnation.current;
      const write = preparePageDraft(owner, scope, latest.current.version, snapshot, codecRef.current, load.current?.epoch ?? "initial", load.current?.record?.revision ?? null);
      return { write, commit: () => {
        if (disposed.current || capturedIncarnation !== incarnation.current || epoch.current !== draftGeneration()) return;
        load.current = { value: snapshot, record: write.record, epoch: write.record.epoch, notice: null };
        saved.current = snapshotFingerprint;
        // Navigation continues after the durable write, before React may render.
        // Publish the same saved snapshot to imperative guards immediately.
        state.current.savedAt = write.record.savedAt;
        setSavedAt(write.record.savedAt); setNotice("saved");
      } };
    },
  };
  useEffect(() => leave.register(() => controller.current), [leave.register]);
  useEffect(() => { leave.changed(); }, [fingerprint, dirty, loaded, busy, formalPending, savedAt, notice, conflict, leave.changed]);
  return { dirty, loaded, savedAt, notice: notice ? draftMessage(notice, locale) : null, conflict, isCurrent, clear, runFormal, formalPending, resetWorking, discardEdits, restoreConflict, requestLeave: (run: () => void) => leave.request(run, [id.current]), controller: () => controller.current };
}
export type PreparedDraft = { write: DraftWrite; commit: () => void };
