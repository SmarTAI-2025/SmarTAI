import { useEffect, useState } from "react";
import { getJSON, normalizeAPIError } from "@/api/client";
import type { SourceDraft } from "@/lib/taskPageDrafts";

export function useProblemDraftReferences(taskId: string | undefined, sources: SourceDraft[], update: (id: string, patch: Partial<SourceDraft>) => void) {
  const [states, setStates] = useState<Record<string, "ready" | "missing" | "unavailable">>({});
  const [attempt, retry] = useState(0);
  const references = sources.map((source) => ({ id: source.id,
    stored: source.sourceMode === "upload" ? source.storedFileId : null,
    library: source.sourceMode === "library" ? source.libraryMaterial?.material_id : null,
    prepared: source.prepared?.operationId,
  }));
  const signature = JSON.stringify(references);
  // Only IDs trigger validation; keystrokes never initiate network work.
  useEffect(() => {
    let cancelled = false;
    const refs = JSON.parse(signature) as typeof references;
    for (const ref of refs) {
      if (!taskId || !(ref.stored || ref.library || ref.prepared)) continue;
      const key = JSON.stringify(ref);
      getJSON<{ available: boolean; filename: string | null; prepared: boolean }>(`/tasks/${taskId}/problem-sources/draft-reference`, {
        params: { stored_file_id: ref.stored || undefined, library_material_id: ref.library || undefined, prepared_id: ref.prepared || undefined },
      }).then((result) => {
        if (cancelled) return;
        setStates((old) => ({ ...old, [key]: "ready" }));
        if (ref.prepared && !result.prepared) update(ref.id, { prepared: null });
      }).catch((error: unknown) => {
        if (cancelled) return;
        const missing = [403, 404, 410].includes(normalizeAPIError(error).status);
        setStates((old) => ({ ...old, [key]: missing ? "missing" : "unavailable" }));
      });
    }
    return () => { cancelled = true; };
    // update is a local form callback; it must not cause a revalidation on every edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, taskId, attempt]);
  const status = (id: string) => {
    const ref = references.find((item) => item.id === id);
    return !ref || !(ref.stored || ref.library || ref.prepared) ? "ready" : states[JSON.stringify(ref)] ?? "checking";
  };
  return { status, blocked: references.some((ref) => status(ref.id) !== "ready"), retry: () => { setStates({}); retry((old) => old + 1); } };
}
