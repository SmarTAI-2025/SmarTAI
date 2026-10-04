import { useRef, useState, type Dispatch, type SetStateAction } from "react";
import { draftGeneration, type DraftCodec } from "@/lib/pageDraftStore";
import { useDraftProtection } from "@/hooks/useDraftProtection";
export { PageDraftSession } from "@/hooks/useDraftProtection";
export function usePageDraft<T extends object>(scope: string, initial: () => T, codec: DraftCodec<T>, version = "", businessBaseline?: T, busy = false) {
  const [baseline, setBaseline] = useState(initial);
  const [value, setValue] = useState(baseline);
  const current = useRef(value); const epoch = useRef(draftGeneration()); const sealed = useRef(false);
  current.current = value;
  const protection = useDraftProtection({ scope, value, baseline: businessBaseline ?? baseline, codec, version, busy, onRestore: (restored) => { current.current = restored; setValue(restored); } });
  function update(next: SetStateAction<T>) {
    if (sealed.current || epoch.current !== draftGeneration()) return;
    const resolved = typeof next === "function" ? (next as (value: T) => T)(current.current) : next;
    if (Object.is(resolved, current.current)) return;
    current.current = resolved; setValue(resolved);
  }
  function field<K extends keyof T>(name: K): [T[K], Dispatch<SetStateAction<T[K]>>] {
    return [value[name], (next) => update((old) => {
      const resolved = typeof next === "function" ? (next as (previous: T[K]) => T[K])(old[name]) : next;
      return Object.is(resolved, old[name]) ? old : { ...old, [name]: resolved };
    })];
  }
  function adoptDefault<K extends keyof T>(name: K, next: T[K]) {
    if (!protection.loaded || current.current[name] || Object.is(current.current[name], next)) return;
    setBaseline((old) => ({ ...old, [name]: next }));
    update((old) => ({ ...old, [name]: next }));
  }
  function clear() { sealed.current = true; void protection.clear(current.current).catch(() => {}); }
  function reset() {
    const snapshot = current.current;
    const empty = initial(); current.current = empty; setValue(empty); sealed.current = false;
    // Keep the draft write lock until IndexedDB deletion and its new revision
    // are both known. New typing is retained while cleanup finishes.
    void protection.runFormal(async () => {}, snapshot).catch(() => {});
  }
  return { value, field, update, adoptDefault, clear, reset, notice: protection.notice, protection };
}
