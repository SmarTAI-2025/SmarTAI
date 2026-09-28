import { compareValues } from "@/lib/sortValues";
import { isTaskProcessing } from "@/lib/taskFlow";
import { useQueries } from "@tanstack/react-query";
import { taskKeys } from "@/api/hooks/keys";
import { getTaskState } from "@/api/tasks";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import {
  useDeleteTask,
  useInterpretTaskHistoryQuery,
  useTags,
  useTaskHistory,
} from "@/api/hooks";
import { HistoryFilters } from "@/components/history/HistoryFilters";
import { HistoryPagination } from "@/components/history/HistoryPagination";
import { HistoryTaskTable } from "@/components/history/HistoryTaskTable";
import {
  applyHistoryInterpretation,
  clearHistoryCondition,
  countHistoryFilters,
  DEFAULT_HISTORY_QUERY,
  parseHistorySearchParams,
  patchHistoryQuery,
  serializeHistoryQuery,
} from "@/components/history/historyQuery";
import { useI18n } from "@/i18n/I18nProvider";
import type { HistoryFacets, HistoryInterpretation, TaskHistoryQuery, TaskLite } from "@/types";

const EMPTY_FACETS: HistoryFacets = {
  semesters: [],
  courses: [],
  tags: [],
  statuses: {},
};

export function HistoryPage() {
  const { locale, t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const query = useMemo(() => parseHistorySearchParams(searchParams), [searchParams]);
  const historyQuery = useTaskHistory(query);
  const tagsQuery = useTags();
  const deleteTask = useDeleteTask();
  const conversation = useRef<string[]>([]);
  const interpretQuery = useInterpretTaskHistoryQuery(() => conversation.current);
  const [manualSort, setManualSort] = useState<TaskHistoryQuery["sort"] | null>(null);
  const [interpretation, setInterpretation] = useState<HistoryInterpretation | null>(null);
  const [preSmartQuery, setPreSmartQuery] = useState<TaskHistoryQuery | null>(null);
  const [smartError, setSmartError] = useState(false);
  const interpretationRequest = useRef(0);
  useEffect(() => () => { interpretationRequest.current += 1; }, []);
  useEffect(() => { interpretationRequest.current += 1; }, [searchParams]);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);

  const data = historyQuery.data;
  const facets = data?.available_facets ?? data?.facets ?? EMPTY_FACETS;
  const tags = tagsQuery.data ?? facets.tags;
  const selectedTasks = interpretation?.execution?.selection?.kind === "tasks" ? interpretation.execution.tasks : undefined;
  const activeSelectedTasks = useMemo(() => selectedTasks?.filter(task => isTaskProcessing(task.status)) ?? [], [selectedTasks]);
  const liveStates = useQueries({
    queries: activeSelectedTasks.map(task => ({
      queryKey: taskKeys.state(task.task_id),
      queryFn: () => getTaskState(task.task_id),
      refetchInterval: (query: { state: { data?: TaskLite } }) => query.state.data && !isTaskProcessing(query.state.data.status) ? false as const : 3_000,
    })),
    combine: results => results.map(result => result.data),
  });
  const liveSelectedTasks = useMemo(() => {
    const statesById = new Map(liveStates.flatMap(state => state ? [[state.task_id, state] as const] : []));
    return selectedTasks?.map(task => {
      const state = statesById.get(task.task_id);
      return state ? { ...task, status: state.status,
        progress_percent: state.progress_percent === undefined ? task.progress_percent : state.progress_percent,
        eta_seconds: state.eta_seconds === undefined ? task.eta_seconds : state.eta_seconds } : task;
    });
  }, [selectedTasks, liveStates]);
  const orderedTasks = useMemo(() => {
    if (!liveSelectedTasks || !manualSort) return liveSelectedTasks;
    const value = (task: TaskLite) => manualSort.startsWith("progress") ? task.progress_percent : manualSort.startsWith("eta") ? task.eta_seconds : manualSort.startsWith("name") ? task.name : manualSort.startsWith("stage") ? task.status : manualSort.startsWith("created") ? task.created_at : task.updated_at;
    return [...liveSelectedTasks].sort((a, b) => compareValues(value(a), value(b), manualSort.endsWith("desc") ? "desc" : "asc"));
  }, [liveSelectedTasks, manualSort]);
  const total = orderedTasks?.length ?? data?.total ?? 0;
  const tasks = orderedTasks ? orderedTasks.slice((query.page - 1) * query.page_size, query.page * query.page_size) : data?.items ?? [];
  const hasFilters = countHistoryFilters(query) > 0;
  const errorMessage = historyQuery.error ? normalizeAPIError(historyQuery.error).message : null;

  useEffect(() => {
    if (!data || total <= 0) return;
    const lastPage = Math.max(1, Math.ceil(total / query.page_size));
    if (query.page > lastPage) {
      setSearchParams(serializeHistoryQuery(patchHistoryQuery(query, { page: lastPage }, { keepPage: true })), { replace: true });
    }
  }, [data, total, query, setSearchParams]);

  function writeQuery(next: TaskHistoryQuery) {
    setSearchParams(serializeHistoryQuery(next), { replace: true });
  }

  function handleChange(patch: Partial<TaskHistoryQuery>, keepPage = false) {
    interpretationRequest.current += 1;
    if (!interpretation?.execution || !(patch.sort || patch.page || patch.page_size)) {
      setInterpretation(null); setPreSmartQuery(null);
    }
    if (patch.sort) setManualSort(patch.sort);
    setSmartError(false);
    writeQuery(patchHistoryQuery(query, patch, { keepPage }));
  }

  function clearAll() {
    conversation.current = []; setManualSort(null);
    interpretationRequest.current += 1;
    setInterpretation(null);
    setPreSmartQuery(null);
    setSmartError(false);
    interpretQuery.reset();
    writeQuery({ ...DEFAULT_HISTORY_QUERY, page_size: query.page_size });
  }

  function cancelInterpretation() {
    interpretationRequest.current += 1;
    interpretQuery.reset();
    setSmartError(false);
  }

  async function handleInterpret(value: string) {
    if (interpretQuery.isPending || !value.trim()) return;
    const request = ++interpretationRequest.current;
    setSmartError(false);
    try {
      const result = await interpretQuery.mutateAsync(value);
      if (request !== interpretationRequest.current) return;
      if (result.ambiguities.length) {
        setInterpretation({ ...result, conditions: [] });
        return;
      }
      setPreSmartQuery(query);
      setInterpretation(result); setManualSort(null);
      if (result.execution) {
        if (result.execution.recognized) conversation.current = [...conversation.current, value].slice(-4);
        writeQuery({ ...DEFAULT_HISTORY_QUERY, page_size: query.page_size });
      } else writeQuery(applyHistoryInterpretation(query, result));
    } catch {
      if (request !== interpretationRequest.current) return;
      setInterpretation(null);
      setSmartError(true);
    }
  }

  function clearSmart() {
    conversation.current = []; setManualSort(null);
    interpretationRequest.current += 1;
    setInterpretation(null);
    setSmartError(false);
    interpretQuery.reset();
    if (preSmartQuery) writeQuery(preSmartQuery);
    setPreSmartQuery(null);
  }

  function handleRemoveCondition(field: string) {
    cancelInterpretation();
    const next = clearHistoryCondition(query, field);
    writeQuery(next);
    setInterpretation((current) => {
      if (!current) return null;
      const conditions = current.conditions.filter((condition) => condition.field !== field);
      return conditions.length ? { ...current, conditions } : null;
    });
  }

  async function handleDelete(task: TaskLite) {
    const confirmed = window.confirm(`${t("historyDeleteConfirmPrefix")}${task.name}${t("historyDeleteConfirmSuffix")}`);
    if (!confirmed) return;
    setDeletingTaskId(task.task_id);
    try {
      await deleteTask.mutateAsync(task.task_id);
      toast.success(t("historyDeleteSuccess"));
    } catch (error) {
      toast.error(normalizeAPIError(error).message);
    } finally {
      setDeletingTaskId(null);
    }
  }

  const countText = historyQuery.isLoading && !data
    ? t("loading")
    : locale === "zh-CN"
      ? `${t("historyFilteredPrefix")}${tasks.length}${t("historyFilteredSeparator")}${total}${t("historyTotalSuffix")}`
      : `Showing ${tasks.length} of ${total} ${total === 1 ? "task" : "tasks"}`;

  return (
    <div className="w-full max-w-[1290px]">
      <header>
        <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">{t("historyTitle")}</h1>
        <p className="mt-1 text-[13px] leading-4 text-muted-foreground">{t("historyDescription")}</p>
      </header>

      <div className="mt-[30px]">
        <HistoryFilters
          query={query}
          courses={facets.courses}
          tags={tags}
          interpretation={interpretation}
          smartError={smartError}
          isInterpreting={interpretQuery.isPending}
          onChange={handleChange}
          onInterpret={(value) => void handleInterpret(value)}
          onCancelInterpret={cancelInterpretation}
          onRemoveCondition={handleRemoveCondition}
          onClearSmart={clearSmart}
          onClear={clearAll}
        />
      </div>

      <div className="mt-6 flex w-full items-center justify-between gap-4 px-1 text-xs text-muted-foreground" aria-live="polite">
        <span>{countText}</span>
        {historyQuery.isFetching && !historyQuery.isLoading ? <span>{t("loading")}</span> : null}
      </div>

      <div className="mt-1">
        <HistoryTaskTable
          tasks={tasks}
          courses={facets.courses}
          tags={tags}
          isLoading={historyQuery.isLoading && !data}
          errorMessage={errorMessage}
          isDeleting={deleteTask.isPending}
          deletingTaskId={deletingTaskId}
          hasFilters={hasFilters}
          sort={interpretation?.execution ? manualSort ?? undefined : query.sort}
          onFilter={handleChange}
          onDelete={(task) => void handleDelete(task)}
          onRetry={() => void historyQuery.refetch()}
          onClear={clearAll}
        />
      </div>

      {!historyQuery.isLoading && !errorMessage ? (
        <HistoryPagination query={query} total={total} onChange={handleChange} />
      ) : null}
    </div>
  );
}
