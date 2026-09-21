import { QueryClientContext } from "@tanstack/react-query";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { interpretFilterIntent } from "@/api/analytics";
import { parseLocalTaskFilter, supportsFilterIntent } from "@/lib/taskFilterIntent";
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

/** The server executes a scoped query; the caller selects records by stable IDs. */
export function useTaskFilterIntent({ taskId, surface, resolveLocal, queryParam = "q", contextKey = "" }: Options) {
  const [params, setParams] = useSearchParams();
  const queryClient = useContext(QueryClientContext);
  // Only exact, whole-command shortcuts run locally; no substring parser may
  // decide that a compound instruction was fully understood.
  const resolve = (value: string) => parseLocalTaskFilter(value, surface);
  const conversationKey = ["ask-conversation", taskId, surface, contextKey];
  const previousQuestions = useRef<string[]>(queryClient?.getQueryData<string[]>(conversationKey) ?? []);
  useEffect(() => {
    previousQuestions.current = queryClient?.getQueryData<string[]>(conversationKey) ?? [];
  }, [taskId, surface, contextKey, queryClient]);
  const taskData = queryClient?.getQueryData<{ workflow_revision?: number; final_result_version?: number }>(["tasks", "detail", taskId]);
  const resultData = queryClient?.getQueryData<{ timestamp?: number }>(["tasks", "result", taskId]);
  const dataVersion = JSON.stringify([taskData?.workflow_revision, taskData?.final_result_version, resultData?.timestamp]);
  const query = params.get(queryParam) ?? "";
  const keyFor = (value: string) => JSON.stringify([taskId, surface, contextKey, dataVersion, value.trim()]);
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

  function writeQuery(value: string, sort?: FilterIntentResult["sort"], ordered = false) {
    const next = new URLSearchParams(paramsRef.current);
    if (value) next.set(queryParam, value); else next.delete(queryParam);
    next.delete("page");
    next.delete("ask_order");
    if (ordered) { next.set("ask_order", "1"); next.delete("sort"); next.delete("column_sort"); }
    if (sort) {
      next.set("sort", sort);
      next.delete("column_sort");
    }
    paramsRef.current = next;
    setParams(next, { replace: true });
  }
  function setQuery(value: string) {
    cancel();
    queryClient?.removeQueries({ queryKey: ["task-filter-intent", key], exact: true });
    queryClient?.removeQueries({ queryKey: ["task-filter-intent", keyFor(value)], exact: true });
    setState({ key: keyFor(value), result: null, pending: false, error: null, source: null });
    if (!value.trim()) { previousQuestions.current = []; queryClient?.removeQueries({ queryKey: conversationKey, exact: true }); }
    writeQuery(value, resolve(value)?.sort);
  }

  async function apply(value = query) {
    cancel();
    const text = value.trim();
    const requestKey = keyFor(text);
    const ticket = generation.current;
    const local = resolve(text);
    queryClient?.removeQueries({ queryKey: ["task-filter-intent", requestKey], exact: true });
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
      const context = { studentId: ["question_analysis", "student_answer_review"].includes(surface) ? contextKey || undefined : undefined,
        history: previousQuestions.current };
      const result = context.studentId || context.history.length
        ? await interpretFilterIntent(taskId, text, surface, abort.signal, context)
        : await interpretFilterIntent(taskId, text, surface, abort.signal);
      if (ticket !== generation.current) return;
      requestRef.current = null;
      const accepted = supportsFilterIntent(result, surface)
        ? result : { ...result, recognized: false };
      const resolution: Resolution = { key: requestKey, result: accepted, pending: false, error: null, source: "model" };
      setState(resolution);
      // Session-owned query cache; revision changes and logout invalidate results.
      if (accepted.recognized) queryClient?.setQueryData(["task-filter-intent", requestKey], resolution);
      if (accepted.recognized) {
        previousQuestions.current = [...previousQuestions.current, text].slice(-4);
        queryClient?.setQueryData(conversationKey, previousQuestions.current);
        if (accepted.sort || accepted.execution?.selection) writeQuery(text, accepted.sort, Boolean(accepted.execution?.selection));
      }
    } catch (error) {
      if (ticket !== generation.current) return;
      requestRef.current = null;
      setState({ key: requestKey, result: null, pending: false, error, source: null });
    }
  }

  const cached = queryClient?.getQueryData<Resolution>(["task-filter-intent", key]);
  const current = state.key === key && (state.result || state.pending || state.error)
    ? state : cached ?? (state.key === key ? state : null);
  // Equivalent local controls keep their identity across navigation/scroll renders.
  const localJson = JSON.stringify(resolve(query));
  const local = useMemo<FilterIntentResult | null>(() => JSON.parse(localJson), [localJson]);
  const result = current?.result ?? local;
  return {
    query, setQuery, apply, cancel,
    execution: current?.result?.execution,
    intent: result?.recognized && supportsFilterIntent(result, surface) ? result : null,
    pending: current?.pending ?? false, error: current?.error ?? null,
    needsApply: Boolean(query.trim() && !result && !current?.pending && !current?.error),
    unrecognized: current?.result?.recognized === false,
    explanation: current?.result?.explanation ?? "",
    source: current?.source ?? (query.trim() && result ? "local" : null),
  };
}
export type TaskFilterController = ReturnType<typeof useTaskFilterIntent>;
