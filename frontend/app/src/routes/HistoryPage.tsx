import { useEffect, useMemo, useState } from "react";
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
  const interpretQuery = useInterpretTaskHistoryQuery();
  const [interpretation, setInterpretation] = useState<HistoryInterpretation | null>(null);
  const [preSmartQuery, setPreSmartQuery] = useState<TaskHistoryQuery | null>(null);
  const [smartError, setSmartError] = useState(false);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);

  const data = historyQuery.data;
  const facets = data?.available_facets ?? data?.facets ?? EMPTY_FACETS;
  const tags = tagsQuery.data ?? facets.tags;
  const total = data?.total ?? 0;
  const tasks = data?.items ?? [];
  const hasFilters = countHistoryFilters(query) > 0;
  const errorMessage = historyQuery.error ? normalizeAPIError(historyQuery.error).message : null;

  useEffect(() => {
    if (!data || data.total <= 0) return;
    const lastPage = Math.max(1, Math.ceil(data.total / query.page_size));
    if (query.page > lastPage) {
      setSearchParams(serializeHistoryQuery(patchHistoryQuery(query, { page: lastPage }, { keepPage: true })), { replace: true });
    }
  }, [data, query, setSearchParams]);

  function writeQuery(next: TaskHistoryQuery) {
    setSearchParams(serializeHistoryQuery(next), { replace: true });
  }

  function handleChange(patch: Partial<TaskHistoryQuery>, keepPage = false) {
    setInterpretation(null);
    setPreSmartQuery(null);
    setSmartError(false);
    writeQuery(patchHistoryQuery(query, patch, { keepPage }));
  }

  function clearAll() {
    setInterpretation(null);
    setPreSmartQuery(null);
    setSmartError(false);
    interpretQuery.reset();
    writeQuery({ ...DEFAULT_HISTORY_QUERY, page_size: query.page_size });
  }

  async function handleInterpret(value: string) {
    setSmartError(false);
    try {
      const result = await interpretQuery.mutateAsync(value);
      setPreSmartQuery(query);
      setInterpretation(result);
      writeQuery(applyHistoryInterpretation(query, result));
    } catch {
      setInterpretation(null);
      setSmartError(true);
    }
  }

  function clearSmart() {
    setInterpretation(null);
    setSmartError(false);
    interpretQuery.reset();
    if (preSmartQuery) writeQuery(preSmartQuery);
    setPreSmartQuery(null);
  }

  function handleRemoveCondition(field: string) {
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
