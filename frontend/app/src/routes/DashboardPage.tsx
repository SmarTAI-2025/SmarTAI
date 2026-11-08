import { userFacingError } from "@/lib/taskActionGuards";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useExperts, useTaskHistory, useTasks } from "@/api/hooks";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { KnowledgeActivityPanel } from "@/components/knowledge-base/KnowledgeActivityPanel";
import { DEFAULT_HISTORY_QUERY } from "@/components/history/historyQuery";
import { TaskListActions, useTaskListDeletion, type TaskListDeletion } from "@/components/history/TaskListActions";
import { formatHistoryEta, formatHistoryTime, taskCourseAndTags, TASK_LIST_COLUMNS } from "@/components/history/historyPresentation";
import { GradingHistoryPanel } from "@/components/history/GradingHistoryPanel";
import { LocalDraftManager } from "@/components/ui/LocalDraftManager";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale, MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { modelDisplayName } from "@/lib/modelPresentation";
import { formatTaskTime, getTaskDestination, isTaskProcessing } from "@/lib/taskFlow";
import { TASK_STAGE_KEYS as STAGE_KEYS, TASK_ACTION_KEYS as ACTION_KEYS, taskStatusTone } from "@/lib/taskPresentation";
import type { ExpertConfig, JobProgress, TaskLite, TaskStatus } from "@/types";

const PROCESSING_STATUSES = new Set<TaskStatus>([
  "extracting_problems",
  "parsing_submissions",
  "grading",
]);

const ACTION_STATUSES = new Set<TaskStatus>([
  "problems_paused", "submissions_paused", "grading_paused",
  "draft",
  "problems_ready",
  "submissions_ready",
  "grading_setup",
  "graded",
  "error",
]);

const STATUS_PRIORITY: Record<TaskStatus, number> = {
  problems_paused: 1,
  submissions_paused: 1,
  grading_paused: 1,
  error: 0,
  problems_ready: 1,
  submissions_ready: 1,
  grading_setup: 1,
  draft: 1,
  extracting_problems: 2,
  parsing_submissions: 2,
  grading: 2,
  graded: 3,
  finalized: 5,
};

type MetricTone = "primary" | "warning" | "accent" | "neutral";

interface DashboardCounts {
  processing: number;
  action: number;
  results: number;
  total: number;
}

export function DashboardPage() {
  const { locale, t } = useI18n();
  const tasksQuery = useTasks();
  const deletion = useTaskListDeletion();
  const expertsQuery = useExperts();

  const tasks = useMemo(
    () => Object.values(tasksQuery.data ?? {}),
    [tasksQuery.data],
  );
  const visibleTasks = useMemo(() => selectVisibleTasks(tasks), [tasks]);
  const counts = useMemo<DashboardCounts>(() => summarizeTasks(tasks), [tasks]);
  const experts = useMemo(
    () => [...(expertsQuery.data ?? [])].sort(compareExperts),
    [expertsQuery.data],
  );
  const enabledExperts = experts.filter((expert) => expert.enabled);
  const hasEnabledByok = enabledExperts.some((expert) => expert.is_shared !== true);
  const hasTaskSnapshot = tasksQuery.data !== undefined;
  const modelValue = expertsQuery.isLoading
    ? "—"
    : expertsQuery.isError
      ? "—"
      : enabledExperts.length > 0
        ? hasEnabledByok
          ? (locale === "zh-CN" ? "已配置" : "Configured")
          : (locale === "zh-CN" ? "平台可用" : "Platform ready")
        : (locale === "zh-CN" ? "进入配置" : "Set up");

  const metrics: Array<{
    label: MessageKey;
    value: string;
    tone: MetricTone;
    to?: string;
  }> = [
    {
      label: "dashboardProcessingTasks",
      value: hasTaskSnapshot ? String(counts.processing) : "—",
      tone: "primary",
    },
    {
      label: "dashboardNeedsAction",
      value: hasTaskSnapshot ? String(counts.action) : "—",
      tone: "warning",
    },
    {
      label: "dashboardGeneratedResults",
      value: hasTaskSnapshot ? String(counts.results) : "—",
      tone: "accent",
    },
    {
      label: "dashboardAvailableModels",
      value: modelValue,
      tone: "primary",
      to: "/settings/byok",
    },
  ];

  const tasksError = tasksQuery.error
    ? userFacingError(tasksQuery.error, locale)
    : null;
  const blockingTasksError = hasTaskSnapshot ? null : tasksError;

  return (
    <div className="w-full max-w-[1290px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {t("workspace")}
      </h1>

      <section
        aria-label={t("dashboardMetrics")}
        className="mt-[14px] grid grid-cols-2 gap-3 md:grid-cols-4 min-[1400px]:grid-cols-[repeat(4,250px)_40px_150px] min-[1400px]:gap-5"
      >
        {metrics.map((metric) => (
          <MetricCard
            key={metric.label}
            label={t(metric.label)}
            value={metric.value}
            tone={metric.tone}
            to={metric.to}
            separator={t("dashboardLabelSeparator")}
          />
        ))}
        <Link
          to="/tasks/new"
          className="col-span-2 inline-flex h-10 items-center justify-center self-center rounded-lg bg-primary px-5 text-[14px] font-semibold text-primary-foreground outline-none transition-colors hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:col-span-4 md:justify-self-end min-[1400px]:col-span-1 min-[1400px]:col-start-6 min-[1400px]:row-start-1 min-[1400px]:w-[150px]"
        >
          {t("dashboardCreateTask")}
        </Link>
      </section>

      <GradingHistoryPanel dashboard deletion={deletion} />

      <section aria-label={t("dashboardAttentionTasks")} className="mt-10">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-base font-semibold">{t("dashboardAttentionTasks")}</h2>
          <Link className="text-sm font-medium text-primary hover:underline" to="/history">
            {t("dashboardViewAllTasks")}
          </Link>
        </div>
        <DashboardTaskTable
          tasks={visibleTasks}
          deletion={deletion}
          isLoading={tasksQuery.isLoading}
          errorMessage={blockingTasksError}
          onRetry={() => void tasksQuery.refetch()}
        />
      </section>

      <div className="mt-8"><LocalDraftManager maxItems={5} showViewAll /></div>

      <div className="mt-8"><KnowledgeActivityPanel compact /></div>

      {deletion.dialog}
      <DashboardInsight
        counts={counts}
        tasks={visibleTasks}
        enabledExperts={enabledExperts}
        modelsLoading={expertsQuery.isLoading}
        modelsError={expertsQuery.isError}
        tasksLoading={tasksQuery.isLoading && !hasTaskSnapshot}
        tasksError={Boolean(blockingTasksError)}
        onTasksRetry={() => void tasksQuery.refetch()}
        locale={locale}
      />
    </div>
  );
}

function MetricCard({
  label,
  value,
  tone,
  to,
  separator,
}: {
  label: string;
  value: string;
  tone: MetricTone;
  to?: string;
  separator: string;
}) {
  const className = cn(
    "flex h-[90px] min-w-0 flex-col justify-center rounded-[10px] border bg-card px-5 text-left outline-none",
    to && "transition-colors hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring",
  );
  const content = (
    <>
      <strong
        className={cn(
          "min-w-0 font-bold tabular-nums",
          to ? "break-words text-base leading-6 sm:text-[28px] sm:leading-[34px]" : "text-[28px] leading-[34px]",
          tone === "primary" && "text-primary",
          tone === "warning" && "text-amber-600 dark:text-amber-400",
          tone === "accent" && "text-teal-600 dark:text-teal-300",
          tone === "neutral" && "text-foreground",
        )}
      >
        {value}
      </strong>
      <span className="mt-1 text-[13px] font-medium leading-4 text-muted-foreground">
        {label}
      </span>
    </>
  );

  return to ? (
    <Link to={to} className={className} aria-label={`${label}${separator}${value}`}>
      {content}
    </Link>
  ) : (
    <div className={className}>{content}</div>
  );
}

const DASHBOARD_TASK_HEADINGS = [
  "historyColumnTask", "dashboardColumnCourseTags", "historyColumnStage", "historyColumnProgress",
  "historyColumnEta", "historyColumnUpdated", "historyColumnNext", "historyColumnActions",
] as const;

function DashboardTaskTable({
  tasks,
  deletion,
  isLoading,
  errorMessage,
  onRetry,
}: {
  tasks: TaskLite[];
  deletion: TaskListDeletion;
  isLoading: boolean;
  errorMessage: string | null;
  onRetry: () => void;
}) {
  const { t } = useI18n();
  const isDesktop = useMediaQuery("(min-width: 1280px)");

  const needsMetadata = tasks.some(task => task.course_id || task.tag_ids?.length);
  const history = useTaskHistory(DEFAULT_HISTORY_QUERY, needsMetadata);
  const facets = history.data?.available_facets ?? history.data?.facets;
  const courseTags = (task: TaskLite) => taskCourseAndTags(task, facets).text;

  if (isDesktop) {
    return (
      <div
        role="table"
        aria-label={t("dashboardAttentionTasks")}
        aria-busy={isLoading}
        className={cn("w-full overflow-hidden rounded-[8px] border text-left [overflow-wrap:anywhere]", TASK_LIST_COLUMNS)}
      >
        <div
          role="row"
          className="col-span-full grid grid-cols-subgrid min-h-[42px] items-center border-b text-[12px] font-semibold leading-[15px] text-muted-foreground"
        >
          {DASHBOARD_TASK_HEADINGS.map(key => <div key={key} role="columnheader"
            className={key === "historyColumnStage" || key === "historyColumnActions" ? "text-center" : "px-3"}>{t(key)}</div>)}
        </div>
        <div role="rowgroup" className="col-span-full grid min-h-55 grid-cols-subgrid bg-card">
          {isLoading ? <DesktopLoadingRows label={t("loading")} /> : null}
          {!isLoading && errorMessage ? (
            <CompactTableMessage
              title={t("dashboardLoadError")}
              description={errorMessage}
              actionLabel={t("retry")}
              onAction={onRetry}
            />
          ) : null}
          {!isLoading && !errorMessage && tasks.length === 0 ? (
            <CompactTableMessage
              title={t("dashboardEmptyTitle")}
              description={t("dashboardEmptyDescription")}
              actionLabel={t("dashboardCreateFirstTask")}
              actionTo="/tasks/new"
            />
          ) : null}
          {!isLoading && !errorMessage
            ? tasks.map((task) => <DesktopTaskRow key={task.task_id} task={task} courseTags={courseTags(task)} deletion={deletion} />)
            : null}
          {!isLoading && !errorMessage && tasks.length > 0
            ? Array.from({ length: Math.max(0, 5 - tasks.length) }, (_, index) => (
                <div key={`empty-row-${index}`} role="presentation" className="col-span-full h-11 border-t bg-card" />
              ))
            : null}
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-2">
      {isLoading ? <MobileLoadingCards /> : null}
      {!isLoading && errorMessage ? (
        <div className="rounded-[10px] border bg-card p-5">
          <p className="font-semibold">{t("dashboardLoadError")}</p>
          <p className="mt-1 text-sm text-muted-foreground">{errorMessage}</p>
          <button
            type="button"
            className="mt-3 text-sm font-semibold text-primary"
            onClick={onRetry}
          >
            {t("retry")}
          </button>
        </div>
      ) : null}
      {!isLoading && !errorMessage && tasks.length === 0 ? (
        <div className="rounded-[10px] border bg-card p-5">
          <p className="font-semibold">{t("dashboardEmptyTitle")}</p>
          <p className="mt-1 text-sm leading-5 text-muted-foreground">
            {t("dashboardEmptyDescription")}
          </p>
          <Link className="mt-3 inline-block text-sm font-semibold text-primary" to="/tasks/new">
            {t("dashboardCreateFirstTask")}
          </Link>
        </div>
      ) : null}
      {!isLoading && !errorMessage
        ? tasks.map((task) => <MobileTaskCard key={task.task_id} task={task} courseTags={courseTags(task)} deletion={deletion} />)
        : null}
    </div>
  );
}

interface DashboardTaskProps {
  task: TaskLite;
  courseTags: string;
  deletion: TaskListDeletion;
}

function DesktopTaskRow({ task, courseTags, deletion }: DashboardTaskProps) {
  const { locale, t } = useI18n();
  const isProcessing = isTaskProcessing(task.status);
  const progressQuery = useTaskProgress(task.task_id, { enabled: isProcessing });
  const progressValue = getProgressValue(task, progressQuery.progress, progressQuery.percent);
  const complete = task.status === "graded" || task.status === "finalized";
  const etaSeconds = complete ? 0 : progressQuery.data?.eta_seconds ?? task.eta_seconds;
  const eta = etaSeconds == null && isProcessing && progressQuery.progress?.phase !== "done"
    ? t("dashboardEtaEstimating")
    : formatHistoryEta(etaSeconds, locale);

  return (
    <div
      role="row"
      className="col-span-full grid grid-cols-subgrid min-h-11 items-center py-1 border-t text-[13px] leading-4 first:border-t-0"
    >
      <div role="cell" className="min-w-0 px-3">
        <Link
          to={getTaskDestination(task)}
          className="block font-semibold text-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
          title={task.name}
        >
          {task.name}
        </Link>
      </div>
      <div role="cell" title={courseTags} className="min-w-0 px-3 text-muted-foreground">
        {courseTags || t("dashboardCourseUnset")}
      </div>
      <div role="cell" className="min-w-0 px-1 text-center">
        <Link to={getTaskDestination(task)} className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <StatusPill status={task.status}>{t(STAGE_KEYS[task.status])}</StatusPill>
        </Link>
      </div>
      <div role="cell" className="px-3 tabular-nums text-muted-foreground">
        {progressValue}
      </div>
      <div role="cell" className="px-3 tabular-nums text-muted-foreground">
        {eta}
      </div>
      <div role="cell" className="px-3 text-muted-foreground">{formatHistoryTime(task.updated_at, locale)}</div>
      <div role="cell" className="min-w-0 px-3">
        <Link
          to={getTaskDestination(task)}
          className="block font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t(ACTION_KEYS[task.status])}
        </Link>
      </div>
      <div role="cell" className="flex justify-center"><TaskListActions task={task} deletion={deletion} /></div>
    </div>
  );
}

function MobileTaskCard({ task, courseTags, deletion }: DashboardTaskProps) {
  const { locale, t } = useI18n();
  const isProcessing = isTaskProcessing(task.status);
  const progressQuery = useTaskProgress(task.task_id, { enabled: isProcessing });
  const progressValue = getProgressValue(task, progressQuery.progress, progressQuery.percent);

  return (
    <article className="relative [overflow-wrap:anywhere]">
      <Link to={getTaskDestination(task)}
        className="grid gap-3 rounded-[10px] border bg-card p-4 outline-none transition-colors hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold">{task.name}</p>
            {courseTags && <p className="mt-1 text-xs text-muted-foreground">{courseTags}</p>}
            <p className="mt-1 text-xs text-muted-foreground">{formatTaskTime(task.updated_at, true, locale)}</p>
          </div>
          <StatusPill status={task.status}>{t(STAGE_KEYS[task.status])}</StatusPill>
        </div>
        <div className="flex min-h-8 items-center justify-between gap-3 pr-[76px] text-xs text-muted-foreground">
          <span>{t("dashboardColumnProgress")}{t("dashboardLabelSeparator")}{progressValue}</span>
          <span className="font-medium text-primary">{t(ACTION_KEYS[task.status])}</span>
        </div>
      </Link>
      <div className="absolute bottom-4 right-4"><TaskListActions task={task} deletion={deletion} /></div>
    </article>
  );
}

function StatusPill({
  status,
  children,
}: {
  status: TaskStatus;
  children: ReactNode;
}) {
  return (
    <span
      title={typeof children === "string" ? children : undefined}
      className={cn(
        "inline-flex min-h-7 max-w-full items-center justify-center rounded-full px-3 py-1 text-center text-[12px] font-semibold xl:text-[13px]",
        taskStatusTone(status),
      )}
    >
      {children}
    </span>
  );
}

function DashboardInsight({
  counts,
  tasks,
  enabledExperts,
  modelsLoading,
  modelsError,
  tasksLoading,
  tasksError,
  onTasksRetry,
  locale,
}: {
  counts: DashboardCounts;
  tasks: TaskLite[];
  enabledExperts: ExpertConfig[];
  modelsLoading: boolean;
  modelsError: boolean;
  tasksLoading: boolean;
  tasksError: boolean;
  onTasksRetry: () => void;
  locale: Locale;
}) {
  const { t } = useI18n();
  const primaryTask = tasks[0];
  const hasEnabledByok = enabledExperts.some((expert) => expert.is_shared !== true);

  return (
    <section className="mt-8 min-h-[170px] rounded-[10px] border bg-card px-5 py-6 sm:px-6 xl:mt-[76px] xl:py-[25px]">
      <h2 className="text-[18px] font-bold leading-[22px] tracking-[-0.01em]">
        {t("dashboardInsightTitle")}
      </h2>
      <p className="mt-[14px] max-w-[1120px] text-[15px] leading-5 text-muted-foreground">
        {buildInsightSummary(
          counts,
          { isLoading: tasksLoading, isError: tasksError },
          {
            isLoading: modelsLoading,
            isError: modelsError,
            hasEnabled: enabledExperts.length > 0,
            hasByok: hasEnabledByok,
          },
          locale,
        )}
      </p>
      <div className="mt-[22px] flex flex-wrap gap-3">
        {tasksLoading ? (
          <span className="inline-flex h-7 items-center rounded-full bg-muted px-4 text-[12px] font-semibold text-muted-foreground">
            {t("loading")}
          </span>
        ) : tasksError ? (
          <button
            type="button"
            onClick={onTasksRetry}
            className="inline-flex h-7 items-center rounded-full bg-blue-100 px-4 text-[12px] font-semibold text-blue-700 outline-none transition-colors hover:bg-blue-200 focus-visible:ring-2 focus-visible:ring-ring dark:bg-blue-950 dark:text-blue-200"
          >
            {t("retry")}
          </button>
        ) : (
          <Link
            to={primaryTask ? getTaskDestination(primaryTask) : "/tasks/new"}
            className="inline-flex h-7 items-center rounded-full bg-blue-100 px-4 text-[12px] font-semibold text-blue-700 outline-none transition-colors hover:bg-blue-200 focus-visible:ring-2 focus-visible:ring-ring dark:bg-blue-950 dark:text-blue-200"
          >
            {primaryTask ? t(ACTION_KEYS[primaryTask.status]) : t("dashboardCreateFirstTask")}
          </Link>
        )}

        {modelsLoading ? (
          <span className="inline-flex h-7 items-center rounded-full bg-muted px-4 text-[12px] font-semibold text-muted-foreground">
            {t("modelsLoading")}
          </span>
        ) : null}

        {!modelsLoading && (modelsError || enabledExperts.length === 0) ? (
          <Link
            to="/settings/byok"
            className="inline-flex h-7 items-center rounded-full bg-amber-100 px-4 text-[12px] font-semibold text-amber-700 outline-none hover:bg-amber-200 focus-visible:ring-2 focus-visible:ring-ring dark:bg-amber-950 dark:text-amber-200"
          >
            {modelsError
              ? (locale === "zh-CN" ? "检查 BYOK 配置" : "Check BYOK setup")
              : (locale === "zh-CN" ? "进入 BYOK 配置" : "Set up BYOK")}
          </Link>
        ) : null}

        {!modelsLoading && !modelsError && enabledExperts.length > 0 ? (
          <Link
            to="/settings/byok"
            title={locale === "zh-CN"
              ? `已启用 ${enabledExperts.length} 个模型配置，点击进入管理`
              : `${enabledExperts.length} model ${enabledExperts.length === 1 ? "configuration is" : "configurations are"} enabled. Open settings to manage them.`}
            className="inline-flex h-7 items-center rounded-full bg-teal-100 px-4 text-[12px] font-semibold text-teal-700 outline-none transition-colors hover:bg-teal-200 focus-visible:ring-2 focus-visible:ring-ring dark:bg-teal-950 dark:text-teal-200"
          >
            {hasEnabledByok
              ? (locale === "zh-CN" ? "BYOK 已配置" : "BYOK configured")
              : (locale === "zh-CN" ? "平台模型可用" : "Platform model available")}
          </Link>
        ) : null}
      </div>
    </section>
  );
}

function CompactTableMessage({
  title,
  description,
  actionLabel,
  actionTo,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel: string;
  actionTo?: string;
  onAction?: () => void;
}) {
  const actionClassName = "mt-3 inline-flex h-7 items-center rounded-md bg-primary px-3 text-xs font-semibold text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring";

  return (
    <div role="row" className="col-span-full h-full">
      <div role="cell" aria-colspan={DASHBOARD_TASK_HEADINGS.length} className="flex h-full flex-col items-center justify-center px-6 text-center">
        <p className="text-sm font-semibold">{title}</p>
        <p className="mt-1 max-w-xl text-xs leading-5 text-muted-foreground">{description}</p>
        {actionTo ? (
          <Link to={actionTo} className={actionClassName}>{actionLabel}</Link>
        ) : (
          <button type="button" className={actionClassName} onClick={onAction}>{actionLabel}</button>
        )}
      </div>
    </div>
  );
}

function DesktopLoadingRows({ label }: { label: string }) {
  return (
    <>
      <span role="status" className="sr-only">{label}</span>
      {Array.from({ length: 4 }, (_, index) => (
        <div
          key={index}
          role="row"
          className="col-span-full grid grid-cols-subgrid h-11 animate-pulse items-center border-t first:border-t-0"
        >
          {DASHBOARD_TASK_HEADINGS.map((_, cell) => (
            <div key={cell} role="cell" className="px-3">
              <div className="h-3 rounded bg-muted" style={{ width: `${48 + ((index + cell) % 4) * 10}%` }} />
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

function MobileLoadingCards() {
  return (
    <div className="grid animate-pulse gap-2">
      {Array.from({ length: 3 }, (_, index) => (
        <div key={index} className="h-[94px] rounded-[10px] border bg-card p-4">
          <div className="h-4 w-2/5 rounded bg-muted" />
          <div className="mt-3 h-3 w-3/5 rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}

function summarizeTasks(tasks: TaskLite[]): DashboardCounts {
  return tasks.reduce<DashboardCounts>(
    (counts, task) => {
      if (PROCESSING_STATUSES.has(task.status)) counts.processing += 1;
      if (ACTION_STATUSES.has(task.status)) counts.action += 1;
      if (["graded", "finalized"].includes(task.status)) counts.results += 1;
      counts.total += 1;
      return counts;
    },
    { processing: 0, action: 0, results: 0, total: 0 },
  );
}

function selectVisibleTasks(tasks: TaskLite[]): TaskLite[] {
  return [...tasks]
    .sort((a, b) => {
      const priority = STATUS_PRIORITY[a.status] - STATUS_PRIORITY[b.status];
      if (priority !== 0) return priority;
      const updated = b.updated_at - a.updated_at;
      if (updated !== 0) return updated;
      return a.task_id.localeCompare(b.task_id);
    })
    .slice(0, 5);
}

function compareExperts(a: ExpertConfig, b: ExpertConfig): number {
  if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
  const aName = modelDisplayName(a);
  const bName = modelDisplayName(b);
  return aName.localeCompare(bName);
}

function getProgressValue(task: TaskLite, progress: JobProgress | null, percent: number): string {
  if (["graded", "finalized"].includes(task.status)) return "100%";
  if (!isTaskProcessing(task.status) || !progress) return "—";
  return `${percent}%`;
}

function buildInsightSummary(
  counts: DashboardCounts,
  tasksState: { isLoading: boolean; isError: boolean },
  modelsState: {
    isLoading: boolean;
    isError: boolean;
    hasEnabled: boolean;
    hasByok: boolean;
  },
  locale: Locale,
): string {
  if (locale === "en-US") {
    const taskSummary = tasksState.isLoading
      ? "Task status is loading."
      : tasksState.isError
        ? "Task status is temporarily unavailable."
        : counts.total === 0
          ? "No grading tasks yet. Create one to start the task workflow."
          : `${counts.action === 1 ? "1 task needs" : `${counts.action} tasks need`} your input, ${counts.processing === 1 ? "1 is" : `${counts.processing} are`} running, and ${counts.results === 1 ? "1 has" : `${counts.results} have`} results ready for review.`;
    const modelSummary = modelsState.isLoading
      ? " Model configuration is loading."
      : modelsState.isError
        ? " Model status is temporarily unavailable."
        : modelsState.hasByok
          ? " BYOK is configured; use the control below to manage it."
          : modelsState.hasEnabled
            ? " A platform model is available; use the control below to review model settings."
          : " BYOK is not configured yet; use the control below to set it up.";
    return taskSummary + modelSummary;
  }

  const taskSummary = tasksState.isLoading
    ? "正在读取任务状态。"
    : tasksState.isError
      ? "任务状态暂时不可用，请重试后再查看。"
      : counts.total === 0
        ? "还没有批改任务。创建任务后，这里会优先整理需要继续、后台处理中和已生成结果的任务。"
        : `${counts.action} 个任务需要你继续，${counts.processing} 个正在后台处理，${counts.results} 个已生成结果可进入复核。`;
  const modelSummary = modelsState.isLoading
    ? " 模型配置正在读取。"
    : modelsState.isError
      ? " 模型状态暂时不可用。"
      : modelsState.hasByok
        ? " BYOK 已配置，可通过下方入口管理。"
        : modelsState.hasEnabled
          ? " 平台模型可用，可通过下方入口查看模型设置。"
        : " 尚未配置 BYOK，可通过下方入口完成配置。";
  return taskSummary + modelSummary;
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}
