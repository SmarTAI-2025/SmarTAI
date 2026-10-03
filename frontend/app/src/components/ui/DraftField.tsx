import { useEffect, type MutableRefObject } from "react";
import { useDraftProtection } from "@/hooks/useDraftProtection";

export type DraftFieldHandle = { clear: (snapshot?: object) => Promise<boolean>; requestLeave: (run: () => void) => void; conflict: boolean };
/** Each formally saved field has its own lifecycle; saving one never clears its siblings. */
export function DraftField<T extends object>({ id, handles, ...options }: {
  id: string; handles: MutableRefObject<Map<string, DraftFieldHandle>>;
  scope: string; value: T; baseline: T; version: string; busy: boolean;
  onRestore: (value: T) => void;
}) {
  const draft = useDraftProtection(options);
  useEffect(() => {
    handles.current.set(id, { clear: (snapshot) => draft.clear(snapshot as T | undefined), requestLeave: draft.requestLeave, conflict: Boolean(draft.conflict) });
    return () => { handles.current.delete(id); };
  });
  return null;
}
