import { userFacingError } from "@/lib/taskActionGuards";
import { compareValues } from "@/lib/sortValues";
import { isTaskProcessing } from "@/lib/taskFlow";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { taskKeys } from "@/api/hooks/keys";
import { getTaskState, listTasks } from "@/api/tasks";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigationType, useSearchParams } from "react-router-dom";
import {
  useInterpretTaskHistoryQuery,
  useTags,
  useTaskHistory,
} from "@/api/hooks";
import { HistoryFilters } from "@/components/history/HistoryFilters";
import { taskHistoryOptions } from "@/api/hooks/tasks";
import type { TaskListDeletion } from "./TaskListActions";
import { HistoryPagination } from "@/components/history/HistoryPagination";
import { HistoryTaskTable } from "@/components/history/HistoryTaskTable";
import {
  applyHistoryInterpretation,
  clearHistoryCondition,
  countHistoryFilters,
  historyKeywordMatches,
  type HistoryKeywordMatch,
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

export function GradingHistoryPanel({ dashboard = false, deletion }: { dashboard?: boolean; deletion: TaskListDeletion }) {
  const { locale, t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const query = useMemo(() => parseHistorySearchParams(searchParams), [searchParams]);
  const latestQuery = useRef(query);
  const location = useLocation();
  const queryClient = useQueryClient();
  const navigationType = useNavigationType();
  const [draft, setDraft] = useState(query.q ?? "");
  const appliedKeyword = useRef<HistoryKeywordMatch["field"] | null>(null);
  const [choices, setChoices] = useState<HistoryKeywordMatch[]>([]);
  const [checkingKeyword, setCheckingKeyword] = useState(false);
  const conversation = useRef<string[]>([]);
  const interpretQuery = useInterpretTaskHistoryQuery(() => conversation.current);
  const [manualSort, setManualSort] = useState<TaskHistoryQuery["sort"] | null>(null);
  const [interpretation, setInterpretation] = useState<HistoryInterpretation | null>(null);
  const [smartError, setSmartError] = useState(false);
  const interpretationRequest = useRef(0);
  useEffect(() => () => { interpretationRequest.current += 1; }, []);
  // A replace written by this panel may commit after the next search starts.
  // Only external navigation (including Back/Forward) resets the current intent.
  useLayoutEffect(() => {
    if (navigationType === "REPLACE" && location.state?.taskHistoryFilters === true) return;
    latestQuery.current = query;
    interpretationRequest.current += 1;
    setCheckingKeyword(false);
    setChoices([]);
    setDraft(query.q ?? "");
    setInterpretation(null);
    setSmartError(false);
    conversation.current = [];
    appliedKeyword.current = null;
  }, [location.key, location.state, navigationType, query]);

  const hasFilters = countHistoryFilters(query) > 0;
  const showResults = !dashboard || hasFilters || Boolean(interpretation?.execution?.selection || interpretation?.conditions.length);
  const historyQuery = useTaskHistory(query, showResults);
  const tagsQuery = useTags(showResults);
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
  const errorMessage = !orderedTasks && historyQuery.error ? userFacingError(historyQuery.error, locale) : null;

  const writeQuery = useCallback((next: TaskHistoryQuery) => {
    latestQuery.current = next;
    setSearchParams(serializeHistoryQuery(next), { replace: true, state: { taskHistoryFilters: true } });
  }, [setSearchParams]);

  useEffect(() => {
    if (!data || historyQuery.isPlaceholderData) return;
    const lastPage = Math.max(1, Math.ceil(total / query.page_size));
    if (query.page > lastPage && serializeHistoryQuery(query).toString() === serializeHistoryQuery(latestQuery.current).toString()) {
      writeQuery(patchHistoryQuery(query, { page: lastPage }, { keepPage: true }));
    }
  }, [data, historyQuery.isPlaceholderData, total, query, writeQuery]);

  function handleChange(patch: Partial<TaskHistoryQuery>, keepPage = false) {
    cancelInterpretation();
    if (!interpretation?.execution || !(patch.sort || patch.page || patch.page_size)) {
      setInterpretation(null);
    }
    if (patch.sort) setManualSort(patch.sort);
    writeQuery(patchHistoryQuery(latestQuery.current, patch, { keepPage }));
  }

  function clearAll() {
    setDraft("");
    setChoices([]);
    setCheckingKeyword(false);
    appliedKeyword.current = null;
    conversation.current = []; setManualSort(null);
    interpretationRequest.current += 1;
    setInterpretation(null);
    setSmartError(false);
    interpretQuery.reset();
    writeQuery({ ...DEFAULT_HISTORY_QUERY, page_size: latestQuery.current.page_size });
  }

  function cancelInterpretation() {
    setChoices([]);
    setCheckingKeyword(false);
    interpretationRequest.current += 1;
    interpretQuery.reset();
    setSmartError(false);
  }

  function keywordBase() {
    // Replace the previous search term while preserving independently linked filters.
    return patchHistoryQuery(latestQuery.current, { q: undefined,
      ...(appliedKeyword.current ? { [appliedKeyword.current]: undefined } : {}) });
  }

  function applyKeyword(match: HistoryKeywordMatch) {
    conversation.current = [];
    setManualSort(null);
    setChoices([]);
    setInterpretation(null);
    setSmartError(false);
    setDraft(match.label);
    const base = keywordBase();
    const filters: Partial<TaskHistoryQuery> = {};
    if (match.field === "q") filters.q = match.id;
    if (match.field === "course_id") filters.course_id = match.id;
    if (match.field === "tag_ids") filters.tag_ids = [match.id];
    appliedKeyword.current = match.field;
    writeQuery(patchHistoryQuery(base, filters));
  }

  async function lookupKeyword(value: string): Promise<HistoryKeywordMatch[]> {
    try {
      const snapshot = await queryClient.fetchQuery({ ...taskHistoryOptions(DEFAULT_HISTORY_QUERY), staleTime: 3_000 });
      const needle = value.toUpperCase().toLowerCase();
      const containsName = (task: TaskLite) => task.name.toUpperCase().toLowerCase().includes(needle);
      let hasNames = snapshot.items.some(containsName);
      if (!hasNames && snapshot.total > snapshot.items.length) {
        const tasks = await queryClient.fetchQuery({ queryKey: taskKeys.list(), queryFn: listTasks, staleTime: 3_000 });
        hasNames = Object.values(tasks).some(containsName);
      }
      return historyKeywordMatches(value, hasNames, snapshot.available_facets ?? snapshot.facets);
    } catch {
      // A directory failure should not prevent the existing Ask endpoint from answering.
      return [];
    }
  }

  async function handleInterpret(input: string) {
    const value = input.trim();
    if (!value || checkingKeyword || interpretQuery.isPending) return;
    const request = ++interpretationRequest.current;
    setSmartError(false);
    setChoices([]);
    try {
      // Look up task metadata without putting a teacher's free-form question in a URL.
      // A large history can use the same full list of summaries as the dashboard.
      if ([...value].length <= 200) {
        setCheckingKeyword(true);
        const matches = await lookupKeyword(value);
        if (request !== interpretationRequest.current) return;
        if (matches.length === 1) {
          applyKeyword(matches[0]);
          return;
        }
        if (matches.length > 1) {
          setChoices(matches);
          return;
        }
      }
      const result = await interpretQuery.mutateAsync(value);
      if (request !== interpretationRequest.current) return;
      if (result.ambiguities.length) {
        setInterpretation({ ...result, conditions: [] });
        return;
      }
      setInterpretation(result);
      appliedKeyword.current = null;
      setManualSort(null);
      if (result.execution) {
        if (result.execution.recognized) conversation.current = [...conversation.current, value].slice(-4);
        if (result.execution.recognized) writeQuery({ ...DEFAULT_HISTORY_QUERY, page_size: latestQuery.current.page_size });
      } else writeQuery(applyHistoryInterpretation(latestQuery.current, result));
    } catch {
      if (request !== interpretationRequest.current) return;
      setInterpretation(null);
      setSmartError(true);
    } finally {
      if (request === interpretationRequest.current) setCheckingKeyword(false);
    }
  }

  function handleRemoveCondition(field: string) {
    cancelInterpretation();
    const next = clearHistoryCondition(latestQuery.current, field);
    writeQuery(next);
    setInterpretation((current) => {
      if (!current) return null;
      const conditions = current.conditions.filter((condition) => condition.field !== field);
      return conditions.length ? { ...current, conditions } : null;
    });
  }

  useEffect(() => {
    const deletedId = deletion.lastDeletedId;
    if (!deletedId) return;
    interpretationRequest.current += 1;
    setCheckingKeyword(false);
    setInterpretation(current => {
      const execution = current?.execution;
      if (!current || !execution) return current;
      if (execution.chart) return null;
      if (execution.selection?.kind !== "tasks") return current;
      return { ...current, execution: { ...execution,
        selection: { ...execution.selection, ids: execution.selection.ids.filter(id => id !== deletedId) },
        tasks: execution.tasks?.filter(item => item.task_id !== deletedId),
      } };
    });
  }, [deletion.lastDeletedId]);

  const loadingTable = !orderedTasks && historyQuery.isLoading && !data;
  const countText = loadingTable
    ? t("loading")
    : locale === "zh-CN"
      ? `${t("historyFilteredPrefix")}${tasks.length}${t("historyFilteredSeparator")}${total}${t("historyTotalSuffix")}`
      : `Showing ${tasks.length} of ${total} ${total === 1 ? "task" : "tasks"}`;

  return (
    <div className="w-full max-w-[1290px]">
      {!dashboard && <header>
        <p className="mt-1 text-[13px] leading-4 text-muted-foreground">{t("historyDescription")}</p>
      </header>}

      <div className={dashboard ? "" : "mt-[30px]"}>
        <HistoryFilters
          dashboard={dashboard}
          value={draft}
          choices={choices}
          onChoose={applyKeyword}
          onNameSearch={() => applyKeyword({ field: "q", id: draft.trim(), label: draft.trim() })}
          canClear={Boolean(draft || hasFilters || interpretation || smartError)}
          onEdit={value => {
            setDraft(value);
            if (!value.trim()) clearAll();
          }}
          courses={facets.courses}
          tags={tags}
          interpretation={interpretation}
          smartError={smartError}
          isInterpreting={checkingKeyword || interpretQuery.isPending}
          onInterpret={(value) => void handleInterpret(value)}
          onCancelInterpret={cancelInterpretation}
          onRemoveCondition={handleRemoveCondition}
          onClear={clearAll}
        />
      </div>

      {showResults && <>
        <div className="mt-6 flex w-full items-center justify-between gap-4 px-1 text-xs text-muted-foreground" aria-live="polite">
          <span>{countText}</span>
          {historyQuery.isFetching && !historyQuery.isLoading ? <span>{t("loading")}</span> : null}
        </div>

        <div className="mt-1">
          <HistoryTaskTable
            tasks={tasks}
            courses={facets.courses}
            tags={tags}
            isLoading={loadingTable}
            errorMessage={errorMessage}
            deletion={deletion}
            hasFilters={hasFilters || Boolean(selectedTasks)}
            sort={interpretation?.execution ? manualSort ?? undefined : query.sort}
            onFilter={handleChange}
            onRetry={() => void historyQuery.refetch()}
          />
        </div>

        {!historyQuery.isLoading && !errorMessage ? (
          <HistoryPagination query={query} total={total} onChange={handleChange} />
        ) : null}
      </>}
    </div>
  );
}
