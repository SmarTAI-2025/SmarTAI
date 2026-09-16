import { useEffect, useRef, useState } from "react";
import { interpretFilterIntent } from "@/api/analytics";
import type { FilterIntentResult, FilterIntentSurface } from "@/types";

/** Only translate the query; all filtering stays with the page's local records. */
export function useTaskFilterIntent({ taskId, query, surface, localIntent, resolveLocalIntent, contextKey = "" }: {
  taskId?: string;
  query: string;
  surface: FilterIntentSurface;
  localIntent: FilterIntentResult | null;
  resolveLocalIntent?: (value: string) => FilterIntentResult | null;
  contextKey?: string;
}) {
  const key = JSON.stringify([taskId, surface, query.trim(), contextKey]);
  const context = useRef(key);
  context.current = key;
  const generation = useRef(0);
  const pendingKey = useRef<string | null>(null);
  const observedKey = useRef(key);
  const [state, setState] = useState<{ key: string; result: FilterIntentResult | null; error: unknown | null; pending: boolean }>({ key, result: null, error: null, pending: false });
  useEffect(() => () => { generation.current += 1; }, []);
  useEffect(() => {
    if (observedKey.current === key) return;
    observedKey.current = key;
    if (pendingKey.current && pendingKey.current !== key) {
      generation.current += 1;
      pendingKey.current = null;
    }
    setState((old) => old.key === key ? old : { key, result: null, error: null, pending: false });
  }, [key]);
  const current = state.key === key ? state : null;
  const result = current?.result;
  const intent = result ? result.recognized ? result : null : localIntent;

  function cancel() {
    generation.current += 1;
    pendingKey.current = null;
    setState((old) => ({ ...old, pending: false, error: null }));
  }

  async function apply(value = query) {
    const requestKey = JSON.stringify([taskId, surface, value.trim(), contextKey]);
    const request = ++generation.current;
    const resolvedLocalIntent = value.trim() === query.trim()
      ? localIntent
      : resolveLocalIntent?.(value) ?? null;
    if (!taskId || !value.trim() || resolvedLocalIntent) {
      pendingKey.current = null;
      setState({ key: requestKey, result: null, error: null, pending: false });
      return;
    }
    pendingKey.current = requestKey;
    setState({ key: requestKey, result: null, error: null, pending: true });
    try {
      const translated = await interpretFilterIntent(taskId, value.trim(), surface);
      if (request !== generation.current) return;
      pendingKey.current = null;
      setState({ key: requestKey, result: translated, error: null, pending: false });
    } catch (error) {
      if (request !== generation.current) return;
      pendingKey.current = null;
      setState({ key: requestKey, result: null, error, pending: false });
    }
  }

  return {
    intent, apply, cancel, pending: current?.pending ?? false, error: current?.error ?? null,
    explanation: result?.explanation ?? "", unrecognized: result?.recognized === false,
    resolution: result ? "llm" as const : localIntent && query.trim() ? "local" as const : null,
  };
}

export type TaskFilterController = ReturnType<typeof useTaskFilterIntent>;
