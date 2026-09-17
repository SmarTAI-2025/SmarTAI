import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { interpretFilterIntent } from "@/api/analytics";
import { supportsFilterIntent } from "@/lib/taskFilterIntent";
import type { FilterIntentResult, FilterIntentSurface } from "@/types";

interface Options {
  taskId?: string;
  surface: FilterIntentSurface;
  resolveLocal: (query: string) => FilterIntentResult | null;
  queryParam?: string;
  contextKey?: string;
}
interface Resolution {
  key: string;
  result: FilterIntentResult | null;
  pending: boolean;
  error: unknown;
  source: "local" | "model" | null;
}

/** Interpretation is instruction-only; the caller always filters its own records. */
export function useTaskFilterIntent({ taskId, surface, resolveLocal, queryParam = "q", contextKey = "" }: Options) {
  const [params, setParams] = useSearchParams();
  const query = params.get(queryParam) ?? "";
  const keyFor = (value: string) => JSON.stringify([taskId, surface, contextKey, value.trim()]);
  const key = keyFor(query);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const generation = useRef(0);
  const requestRef = useRef<{ key: string; abort: AbortController } | null>(null);
  const [state, setState] = useState<Resolution>({ key, result: null, pending: false, error: null, source: null });
  const manualSort = JSON.stringify([params.get("sort"), params.get("column_sort")]);
  const previousSort = useRef(manualSort);

  function cancel() {
    generation.current += 1;
    requestRef.current?.abort.abort();
    requestRef.current = null;
    setState((old) => old.pending || old.error ? { ...old, pending: false, error: null } : old);
  }
  useEffect(() => {
    if (requestRef.current && (requestRef.current.key !== key || previousSort.current !== manualSort)) cancel();
    previousSort.current = manualSort;
  }, [key, manualSort]);
  useEffect(() => () => { generation.current += 1; requestRef.current?.abort.abort(); }, []);

  function writeQuery(value: string, sort?: FilterIntentResult["sort"]) {
    const next = new URLSearchParams(paramsRef.current);
    if (value) next.set(queryParam, value); else next.delete(queryParam);
    next.delete("page");
    if (sort) {
      next.set("sort", sort);
      next.delete("column_sort");
    }
    paramsRef.current = next;
    setParams(next, { replace: true });
  }
  function setQuery(value: string) { cancel(); writeQuery(value, resolveLocal(value)?.sort); }

  async function apply(value = query) {
    cancel();
    const text = value.trim();
    const requestKey = keyFor(text);
    const ticket = generation.current;
    const local = resolveLocal(text);
    writeQuery(text);
    if (!text || local) {
      setState({ key: requestKey, result: local, pending: false, error: null, source: text ? "local" : null });
      if (local?.sort) writeQuery(text, local.sort);
      return;
    }
    if (!taskId) return;
    const abort = new AbortController();
    requestRef.current = { key: requestKey, abort };
    setState({ key: requestKey, result: null, pending: true, error: null, source: null });
    try {
      const result = await interpretFilterIntent(taskId, text, surface, abort.signal);
      if (ticket !== generation.current) return;
      requestRef.current = null;
      const accepted = supportsFilterIntent(result, surface)
        ? result : { ...result, recognized: false };
      setState({ key: requestKey, result: accepted, pending: false, error: null, source: "model" });
      if (accepted.recognized && accepted.sort) writeQuery(text, accepted.sort);
    } catch (error) {
      if (ticket !== generation.current) return;
      requestRef.current = null;
      setState({ key: requestKey, result: null, pending: false, error, source: null });
    }
  }

  const current = state.key === key ? state : null;
  // Equivalent local controls keep their identity across navigation/scroll renders.
  const localJson = JSON.stringify(resolveLocal(query));
  const local = useMemo<FilterIntentResult | null>(() => JSON.parse(localJson), [localJson]);
  const result = current?.result ?? local;
  return {
    query, setQuery, apply, cancel,
    intent: result?.recognized && supportsFilterIntent(result, surface) ? result : null,
    pending: current?.pending ?? false, error: current?.error ?? null,
    unrecognized: current?.result?.recognized === false,
    explanation: current?.result?.explanation ?? "",
    source: current?.source ?? (query.trim() && result ? "local" : null),
  };
}
export type TaskFilterController = ReturnType<typeof useTaskFilterIntent>;
