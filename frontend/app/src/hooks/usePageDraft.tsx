import { createContext, useContext, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { draftGeneration, readPageDraft, removePageDraft, writePageDraft, type DraftCodec } from "@/lib/pageDraftStore";

const DraftOwner = createContext<string | null>(null);

export function PageDraftSession({ ownerId, children }: { ownerId: string; children: ReactNode }) {
  return <DraftOwner.Provider key={ownerId} value={ownerId}>{children}</DraftOwner.Provider>;
}

/** Scope changes must remount the form (task + course + server revision). */
export function usePageDraft<T extends object>(scope: string, initial: () => T, codec: DraftCodec<T>) {
  const owner = useContext(DraftOwner);
  const [loaded] = useState(() => owner ? readPageDraft(owner, scope, codec) : { value: null, notice: null });
  const [value, setValue] = useState<T>(() => loaded.value ?? initial());
  const [notice, setNotice] = useState(loaded.notice);
  const current = useRef(value);
  const epoch = useRef(draftGeneration());
  const sealed = useRef(false);

  function update(next: SetStateAction<T>) {
    if (sealed.current || epoch.current !== draftGeneration()) return;
    const resolved = typeof next === "function" ? (next as (value: T) => T)(current.current) : next;
    if (Object.is(resolved, current.current)) return;
    current.current = resolved;
    setValue(resolved);
    if (owner && !writePageDraft(owner, scope, resolved, codec)) setNotice("memory");
  }

  function field<K extends keyof T>(name: K): [T[K], Dispatch<SetStateAction<T[K]>>] {
    return [value[name], (next) => update((old) => {
      const resolved = typeof next === "function" ? (next as (previous: T[K]) => T[K])(old[name]) : next;
      return Object.is(old[name], resolved) ? old : { ...old, [name]: resolved };
    })];
  }

  function clear() {
    sealed.current = true; // Late upload/recognition callbacks cannot resurrect a submitted draft.
    if (owner) removePageDraft(owner, scope);
  }

  function reset() {
    clear();
    const empty = initial();
    current.current = empty;
    setValue(empty);
    setNotice(null);
    sealed.current = false;
  }

  return { value, field, update, clear, reset, notice };
}
