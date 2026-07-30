import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { normalizeAPIError } from "@/api/client";
import { useExperts, useTasks } from "@/api/hooks";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale, MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { modelDisplayName } from "@/lib/modelPresentation";
import { formatTaskTime, getTaskDestination, isTaskProcessing } from "@/lib/taskFlow";
import type { ExpertConfig, JobProgress, TaskLite, TaskStatus } from "@/types";

const PROCESSING_STATUSES = new Set<TaskStatus>([
  "extracting_problems",
  "parsing_submissions",
  "grading",
  "generating_analysis",
]);

const ACTION_STATUSES = new Set<TaskStatus>([
  "draft",
  "problems_ready",
  "submissions_ready",
  "graded",
  "error",
]);

const STATUS_PRIORITY: Record<TaskStatus, number> = {
  error: 0,
  problems_ready: 1,
  submissions_ready: 1,
  draft: 1,
  extracting_problems: 2,
  parsing_submissions: 2,
  grading: 2,
  graded: 3,
  review_confirmed: 4,
  generating_analysis: 2,
  finalized: 5,
};

const STAGE_KEYS: Record<TaskStatus, MessageKey> = {
  draft: "dashboardStageDraft",
  extracting_problems: "dashboardStageExtracting",
  problems_ready: "dashboardStageProblemsReady",
  parsing_submissions: "dashboardStageParsing",
  submissions_ready: "dashboardStageSubmissionsReady",
  grading: "dashboardStageGrading",
  graded: "dashboardStageGraded",
  review_confirmed: "dashboardStageReviewConfirmed",
  generating_analysis: "dashboardStageGeneratingAnalysis",
  finalized: "dashboardStageFinalized",
  error: "dashboardStageError",
};

const ATTENTION_KEYS: Record<TaskStatus, MessageKey> = {
  draft: "dashboardAttentionAddProblems",
  extracting_problems: "dashboardAttentionRecognizing",
  problems_ready: "dashboardAttentionReviewProblems",
  parsing_submissions: "dashboardAttentionRecognizing",
  submissions_ready: "dashboardAttentionReviewSubmissions",
  grading: "dashboardAttentionGrading",
  graded: "dashboardAttentionReviewResults",
  review_confirmed: "dashboardAttentionResultReady",
  generating_analysis: "dashboardAttentionAnalysisGenerating",
  finalized: "dashboardAttentionFinalized",
  error: "dashboardAttentionResolveError",
};

const ACTION_KEYS: Record<TaskStatus, MessageKey> = {
  draft: "dashboardActionContinue",
  extracting_problems: "dashboardActionViewProgress",
  problems_ready: "dashboardActionReviewProblems",
  parsing_submissions: "dashboardActionViewProgress",
  submissions_ready: "dashboardActionReviewSubmissions",
  grading: "dashboardActionViewProgress",
  graded: "dashboardActionViewResults",
  review_confirmed: "dashboardActionOpenResults",
  generating_analysis: "dashboardActionOpenResults",
  finalized: "dashboardActionOpenResults",
  error: "dashboardActionResolveError",
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
    ? normalizeAPIError(tasksQuery.error).message
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

      <section aria-label={t("dashboardAttentionTasks")} className="mt-10">
        <DashboardTaskTable
          tasks={visibleTasks}
          isLoading={tasksQuery.isLoading}
          errorMessage={blockingTasksError}
          onRetry={() => void tasksQuery.refetch()}
        />
      </section>

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
          "text-[28px] font-bold leading-[34px] tabular-nums",
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

function DashboardTaskTable({
  tasks,
  isLoading,
  errorMessage,
  onRetry,
}: {
  tasks: TaskLite[];
  isLoading: boolean;
  errorMessage: string | null;
  onRetry: () => void;
}) {
  const { t } = useI18n();
  const isDesktop = useMediaQuery("(min-width: 1280px)");

  if (isDesktop) {
    return (
      <div
        role="table"
        aria-label={t("dashboardAttentionTasks")}
        aria-busy={isLoading}
        className="w-[1090px] max-w-full text-left"
      >
        <div
          role="row"
          className="grid h-[42px] grid-cols-[230px_150px_140px_130px_130px_140px_170px] items-center border-b text-[12px] font-semibold leading-[15px] text-muted-foreground"
        >
          <div role="columnheader" className="px-3">{t("dashboardColumnTask")}</div>
          <div role="columnheader" className="px-3">{t("dashboardColumnCourseTags")}</div>
          <div role="columnheader" className="px-3">{t("dashboardColumnStage")}</div>
          <div role="columnheader" className="px-3">{t("dashboardColumnProgress")}</div>
          <div role="columnheader" className="px-3">{t("dashboardColumnEta")}</div>
          <div role="columnheader" className="px-3">{t("dashboardColumnAttention")}</div>
          <div role="columnheader" className="px-3">{t("dashboardColumnNext")}</div>
        </div>
        <div role="rowgroup" className="h-44 overflow-hidden rounded-b-[8px] border-x border-b bg-card">
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
            ? tasks.map((task) => <DesktopTaskRow key={task.task_id} task={task} />)
            : null}
          {!isLoading && !errorMessage && tasks.length > 0
            ? Array.from({ length: Math.max(0, 4 - tasks.length) }, (_, index) => (
                <div key={`empty-row-${index}`} role="presentation" className="h-11 border-t bg-card" />
              ))
            : null}
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-2">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-[15px] font-semibold">{t("dashboardAttentionTasks")}</h2>
        <Link className="text-[13px] font-medium text-primary" to="/history">
          {t("history")}
        </Link>
      </div>
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
        ? tasks.slice(0, 3).map((task) => <MobileTaskCard key={task.task_id} task={task} />)
        : null}
    </div>
  );
}

function DesktopTaskRow({ task }: { task: TaskLite }) {
  const { t } = useI18n();
  const isProcessing = isTaskProcessing(task.status);
  const progressQuery = useTaskProgress(task.task_id, { enabled: isProcessing });
  const progressValue = getProgressValue(task, progressQuery.progress, progressQuery.percent);
  const eta = isProcessing && progressQuery.progress?.phase !== "done"
    ? t("dashboardEtaEstimating")
    : "—";

  return (
    <div
      role="row"
      className="grid h-11 grid-cols-[230px_150px_140px_130px_130px_140px_170px] items-center border-t text-[13px] leading-4 first:border-t-0"
    >
      <div role="cell" className="min-w-0 px-3">
        <Link
          to={getTaskDestination(task)}
          className="block truncate font-semibold text-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
          title={task.name}
        >
          {task.name}
        </Link>
      </div>
      <div role="cell" className="truncate px-3 text-muted-foreground">
        {t("dashboardCourseUnset")}
      </div>
      <div role="cell" className="px-3">
        <StatusPill status={task.status}>{t(STAGE_KEYS[task.status])}</StatusPill>
      </div>
      <div role="cell" className="px-3 tabular-nums text-muted-foreground">
        {progressValue}
      </div>
      <div role="cell" className="px-3 tabular-nums text-muted-foreground">
        {eta}
      </div>
      <div role="cell" className="px-3">
        <AttentionPill status={task.status}>{t(ATTENTION_KEYS[task.status])}</AttentionPill>
      </div>
      <div role="cell" className="min-w-0 px-3">
        <Link
          to={getTaskDestination(task)}
          className="block truncate font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t(ACTION_KEYS[task.status])}
        </Link>
      </div>
    </div>
  );
}

function MobileTaskCard({ task }: { task: TaskLite }) {
  const { locale, t } = useI18n();
  const isProcessing = isTaskProcessing(task.status);
  const progressQuery = useTaskProgress(task.task_id, { enabled: isProcessing });
  const progressValue = getProgressValue(task, progressQuery.progress, progressQuery.percent);

  return (
    <Link
      to={getTaskDestination(task)}
      className="grid gap-3 rounded-[10px] border bg-card p-4 outline-none transition-colors hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{task.name}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {formatTaskTime(task.updated_at, true, locale)}
          </p>
        </div>
        <StatusPill status={task.status}>{t(STAGE_KEYS[task.status])}</StatusPill>
      </div>
      <div className="flex items-center justify-between gap-4 text-xs text-muted-foreground">
        <span>{t("dashboardColumnProgress")}{t("dashboardLabelSeparator")}{progressValue}</span>
        <span className="font-medium text-primary">{t(ACTION_KEYS[task.status])}</span>
      </div>
    </Link>
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
      className={cn(
        "inline-flex h-7 min-w-[112px] max-w-[116px] items-center overflow-hidden text-ellipsis whitespace-nowrap rounded-full px-3 text-[12px] font-semibold xl:text-[13px]",
        PROCESSING_STATUSES.has(status) && "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-200",
        (status === "draft" || status === "problems_ready" || status === "submissions_ready") &&
          "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-200",
        ["graded", "review_confirmed", "finalized"].includes(status) && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-200",
        status === "error" && "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200",
      )}
    >
      {children}
    </span>
  );
}

function AttentionPill({
  status,
  children,
}: {
  status: TaskStatus;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-7 min-w-[112px] max-w-[116px] items-center overflow-hidden text-ellipsis whitespace-nowrap rounded-full px-3 text-[12px] font-semibold xl:text-[13px]",
        status === "error" && "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200",
        status !== "error" && !["graded", "review_confirmed", "finalized"].includes(status) && "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-200",
        ["graded", "review_confirmed", "finalized"].includes(status) && "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-200",
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
    <div role="row" className="h-full">
      <div role="cell" aria-colspan={7} className="flex h-full flex-col items-center justify-center px-6 text-center">
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
          className="grid h-11 animate-pulse grid-cols-[230px_150px_140px_130px_130px_140px_170px] items-center border-t first:border-t-0"
        >
          {Array.from({ length: 7 }, (__, cell) => (
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
      if (["graded", "review_confirmed", "generating_analysis", "finalized"].includes(task.status)) counts.results += 1;
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
    .slice(0, 4);
}

function compareExperts(a: ExpertConfig, b: ExpertConfig): number {
  if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
  const aName = modelDisplayName(a);
  const bName = modelDisplayName(b);
  return aName.localeCompare(bName);
}

function getProgressValue(task: TaskLite, progress: JobProgress | null, percent: number): string {
  if (["graded", "review_confirmed", "finalized"].includes(task.status)) return "100%";
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
