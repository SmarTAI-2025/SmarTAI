import { taskWorkspacePath } from "@/lib/frontierSession";
import { AlertTriangle, CheckCircle2, LoaderCircle } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { continueTaskRun } from "@/api/tasks";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { useStartGrading, useTask } from "@/api/hooks/tasks";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { TaskExecutionControls } from "@/components/tasks/TaskExecutionControls";
import { TaskPausedState } from "@/components/tasks/TaskPausedState";
import { GradingActivity, ModelProgressRows } from "@/components/tasks/GradingActivity";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useGradingIntent } from "@/hooks/useGradingIntent";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { gradingProgressText as copy } from "@/lib/gradingProgressCopy";
import { backgroundErrorTitle, classifyRecoverableError } from "@/lib/taskActionGuards";
import {
  getTaskDestination,
  isTaskAwaitingGrading,
  getTaskGradingSetupHref,
  hasTaskCompletedGrading,
  hasTaskReachedStep,
} from "@/lib/taskFlow";
import type { JobProgress, TaskStatus } from "@/types";

/** C03: factual, resumable grading progress without exposing student identifiers. */
export function GradingProgressPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
  const progressQuery = useTaskProgress(taskId);
  const retryGrading = useStartGrading();
  const intent = useGradingIntent();
  const [manualBusy, setManualBusy] = useState(false);
  const [manualError, setManualError] = useState(false);
  const task = taskQuery.data;
  const state = progressQuery.data;
  const status = (state?.status ?? task?.status) as TaskStatus | undefined;
  const progress = progressQuery.progress;
  const completedView = hasTaskCompletedGrading(status);

  const queue = useMemo(
    () => {
      const derived = deriveQueue(progress, state?.problem_count ?? task?.problem_count ?? 0, state?.student_count ?? task?.student_count ?? 0);
      return completedView
        ? { ...derived, completed: derived.total, running: 0, queued: 0 }
        : derived;
    },
    [completedView, progress, state?.problem_count, state?.student_count, task?.problem_count, task?.student_count],
  );
  const eta = useMemo(() => progressQuery.isError ? copy(locale, "estimating") : estimateRemaining(progress, queue.total, queue.completed, locale), [locale, progress, progressQuery.isError, progressQuery.dataUpdatedAt, queue.completed, queue.total]);
  const latestError = progress?.error_detail
    ?? [...(progress?.messages ?? [])].reverse().find((event) => event.level === "error")?.message
    ?? state?.error
    ?? task?.error
    ?? null;

  if (taskId && isTaskAwaitingGrading(status)) {
    return <Navigate replace to={`/tasks/${taskId}/grading/preflight`} />;
  }
  if (taskId && completedView) {
    return <Navigate replace to={`/tasks/${taskId}/review`} />;
  }
  if (taskId && task && status && !hasTaskReachedStep(task, 5)) {
    return <Navigate replace to={getTaskDestination(task)} />;
  }
  if (taskId && status === "error" && task?.last_failed_job_id && task.last_failed_job_id !== task.grading_job_id) {
    return <Navigate replace to={getTaskDestination(task)} />;
  }

  const refresh = () => {
    retryGrading.reset();
    void Promise.all([taskQuery.refetch(), progressQuery.refetch()]);
  };

  async function handleRetry() {
    if (!taskId || !task) return;
    try {
      const response = await intent.execute(state?.workflow_revision ?? task.workflow_revision, (request) => retryGrading.mutateAsync({ taskId, ...request, retryScope: "failed_only" }));
      if (!response) return;
      if (response.status === "already_done" || (response.status === "already_finished" && ["completed", "partial_failed"].includes(response.run_status ?? ""))) {
        navigate(`/tasks/${taskId}/review`, { replace: true });
        return;
      }
      await Promise.all([taskQuery.refetch(), progressQuery.refetch()]);
    } catch {
      // Error is rendered in the recovery strip without losing task context.
    }
  }

  async function handleManual() {
    if (!taskId || !task?.grading_job_id || manualBusy) return;
    setManualBusy(true);
    setManualError(false);
    try {
      await continueTaskRun(taskId, task.grading_job_id, state?.workflow_revision ?? task.workflow_revision, true);
      await taskQuery.refetch();
      navigate(`/tasks/${taskId}/review`);
    } catch {
      setManualError(true);
      await Promise.all([taskQuery.refetch(), progressQuery.refetch()]);
    } finally { setManualBusy(false); }
  }

  const hasReadableState = Boolean(status || task || state);
  const readFailed = !hasReadableState && (taskQuery.isError || progressQuery.isError);
  const isFinalizing = status === "grading" && progress?.phase === "done";
  const waitingLabel = state?.active_operation_status === "pending" ? (locale === "zh-CN" ? "排队中" : "Queued") : null;
  const percent = completedView ? 100 : state?.active_operation_status === "pending" ? null : progress ? progressQuery.percent : null;
  const recoveryInfo = status === "error" || (status === "grading_paused" && retryGrading.error)
    ? classifyRecoverableError(retryGrading.error ?? latestError, {
      locale,
      phase: progress?.current_step ?? progress?.phase ?? "grading",
      jobId: task?.last_failed_job_id ?? task?.grading_job_id,
      taskId,
      returnTo: `/tasks/${taskId}/grading/progress`,
    })
    : null;

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {status === "grading_paused" ? (locale === "zh-CN" ? "批改进度" : "Grading progress") : copy(locale, "title")}
      </h1>
      <NewTaskStepper currentStep={5} />
      <div className="mx-auto mt-[25px] w-full max-w-[940px]">
      <TaskExecutionControls taskId={taskId} state={state} progress={progress}
        onChanged={() => Promise.all([taskQuery.refetch(), progressQuery.refetch()])} />

      {!taskId ? (
        <PageState
          title={copy(locale, "missingTask")}
          action={copy(locale, "viewHistory")}
          href="/history"
        />
      ) : readFailed ? (
        <RecoverableActionState info={classifyRecoverableError(progressQuery.error ?? taskQuery.error, { locale, taskId, returnTo: `/tasks/${taskId}/grading/progress` })} locale={locale} workflowRecovery={{ retry: { label: locale === "zh-CN" ? "刷新状态" : "Refresh status", onClick: refresh }, configurationHref: getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/progress`) }} />
      ) : !status ? (
        <PageState title={copy(locale, "reading")} busy />
      ) : (
        <div>
              {manualError ? <p role="alert">{locale === "zh-CN" ? "暂时无法进入复核，请刷新状态后重试。" : "Could not open review. Refresh and try again."}</p> : null}
          {status === "error" ? <p className="mb-4 text-sm text-muted-foreground">{locale === "zh-CN" ? "仅重试缺少有效结果的题次，保留成功结果及教师修改。可先在模型配置中调整模型；返回批改设置再开始会重新批改整批。" : "Retry only items without a valid result; keep successful results and teacher edits. You can adjust models first. Starting from grading settings reruns the full batch."}</p> : null}
          {status === "grading_paused" && !retryGrading.error ? (
            <TaskPausedState status="grading_paused" onContinue={() => void handleRetry()} onManual={() => void handleManual()} busy={retryGrading.isPending || manualBusy} manualBusy={manualBusy}>
              <p className="text-sm text-muted-foreground">{locale === "zh-CN" ? "已保存 AI 评分" : "Saved AI grades"} {progress?.saved_units ?? queue.completed} / {queue.total} {copy(locale, "unitSuffix")} · {locale === "zh-CN" ? "已汇总" : "Aggregated"} {queue.completed} / {queue.total}</p>
              <ModelProgressRows models={progress?.model_progress ?? []} locale={locale} stopped />
            </TaskPausedState>
          ) : recoveryInfo ? (
            <RecoverableActionState
              info={recoveryInfo}
              locale={locale}
              usageContext={{ gradingTaskId: taskId }}
              className="min-h-[300px]"
              workflowRecovery={{ retry: { onClick: () => void handleRetry(), busy: retryGrading.isPending, disabled: manualBusy }, configurationHref: getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/progress`) }}
              additionalActions={[{ label: copy(locale, "refresh"), onClick: refresh, busy: retryGrading.isPending, disabled: manualBusy }, { label: manualBusy ? (locale === "zh-CN" ? "正在打开…" : "Opening…") : (locale === "zh-CN" ? "人工继续处理" : "Continue manually"), onClick: () => void handleManual(), busy: manualBusy, disabled: retryGrading.isPending }]}
            />
          ) : (
            <>
              <section className="relative flex min-h-[220px] flex-col overflow-hidden rounded-[10px] border bg-card px-5 pb-5 pt-7 sm:px-10 sm:pb-5 sm:pt-8" aria-live="polite" aria-busy={completedView ? undefined : true}>
                <SmarTAIMascot variant="grading" size="md" className="absolute right-7 top-4 hidden sm:inline-flex" />
                <h2 className="text-[22px] font-bold leading-8 tracking-[-0.01em] text-foreground sm:pr-32 sm:text-[24px]">
                  {completedView
                    ? (locale === "en-US" ? "Grading Completed" : "批改已完成")
                    : waitingLabel ?? copy(locale, isFinalizing ? "finalizing" : "gradingAnswers")}
                </h2>
                <p className="mt-1 text-[13px] leading-5 text-muted-foreground sm:pr-32 sm:text-[14px]">
                  {copy(locale, "completedPrefix")} {queue.completed} / {queue.total} {copy(locale, "unitSuffix")}
                  <span aria-hidden="true"> · </span>
                  {completedView
                    ? (locale === "en-US" ? "Historical progress snapshot" : "历史进度快照")
                    : waitingLabel ?? `${copy(locale, "etaPrefix")} ${isFinalizing ? copy(locale, "almostDone") : eta}`}
                </p>
                <ModelProgressRows models={progress?.model_progress ?? []} locale={locale} />
                {progress?.saved_units !== undefined ? <p className="mt-1 text-xs text-muted-foreground">{locale === "zh-CN" ? "已保存至少一个模型评分的题次" : "Responses with at least one saved model grade"}: {progress.saved_units} / {queue.total}</p> : null}

                <div className="mt-4 flex items-center gap-3">
                  <div
                    className="relative h-2 flex-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
                    role="progressbar"
                    aria-label={copy(locale, "title")}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={percent ?? undefined}
                    aria-valuetext={percent === null ? copy(locale, "syncing") : undefined}
                  >
                    {percent === null ? (
                      waitingLabel ? null : <span className="absolute inset-y-0 left-0 w-1/3 animate-pulse rounded-full bg-primary" />
                    ) : (
                      <span className="block h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: `${percent}%` }} />
                    )}
                  </div>
                  <span className="w-14 shrink-0 text-right text-[18px] font-bold leading-6 text-primary sm:text-[20px]">
                    {percent === null ? "—" : `${percent}%`}
                  </span>
                </div>

                <div className="mt-auto flex flex-col gap-3 pt-5 sm:flex-row sm:items-center sm:justify-between">
                  <span className="inline-flex min-h-7 w-fit items-center rounded-full bg-teal-100 px-3 text-[11px] font-semibold text-teal-700 dark:bg-teal-950/60 dark:text-teal-300 sm:text-[12px]">
                    {completedView ? (locale === "en-US" ? "Completed" : "已完成") : copy(locale, "backgroundChip")}
                  </span>
                  <Link to={completedView ? `/tasks/${taskId}/review` : taskWorkspacePath(taskId)} className="inline-flex h-10 w-full items-center justify-center rounded-[8px] border bg-card px-5 text-[13px] font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring sm:w-[150px] sm:text-[14px]">
                    {completedView ? (locale === "en-US" ? "View Grading Review" : "查看复核批改") : copy(locale, "backWorkspace")}
                  </Link>
                </div>
              </section>

              {progress?.job_id && <GradingActivity key={progress.job_id} taskId={taskId} progress={progress} locale={locale} />}

              <QueueTable locale={locale} queue={queue} completed={completedView} />

              {latestError ? <section className="mt-4 flex min-h-[74px] items-center rounded-[10px] border bg-transparent px-5 py-4 text-[13px] leading-5 text-muted-foreground sm:px-[30px] sm:text-[14px]">
                  <span className="flex items-start gap-2 text-danger">
                    <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                    {backgroundErrorTitle(latestError, locale)}
                  </span>
              </section> : null}
            </>
          )}
          {status === "error" && progress?.job_id && <>
            <ModelProgressRows models={progress.model_progress ?? []} locale={locale} stopped />
            <GradingActivity key={progress.job_id} taskId={taskId} progress={progress} locale={locale} stopped />
          </>}
        </div>
      )}
      </div>
    </div>
  );
}

type QueueSummary = {
  total: number;
  completed: number;
  running: number;
  queued: number;
  errorSignals: number;
};

function deriveQueue(progress: JobProgress | null, problemCount: number, studentCount: number): QueueSummary {
  const total = progress && progress.total_students > 0 && progress.total_questions > 0
    ? progress.total_students * progress.total_questions
    : Math.max(0, problemCount) * Math.max(0, studentCount);
  const completed = Math.min(total, Math.max(0, progress?.completed_units ?? 0));
  const activePairs = new Set(
    (progress?.active ?? []).map((unit) => `${unit.student_id}\u0000${unit.q_id}`),
  );
  const running = Math.min(Math.max(0, total - completed), progress?.running_units ?? activePairs.size);
  const queued = Math.max(0, total - completed - running);
  const eventErrors = (progress?.messages ?? []).filter((event) => event.level === "error").length;
  const errorSignals = eventErrors + (progress?.error_detail && eventErrors === 0 ? 1 : 0);
  return { total, completed, running, queued, errorSignals };
}

function estimateRemaining(progress: JobProgress | null, total: number, completed: number, locale: Locale): string {
  if (!progress || total <= 0) return copy(locale, "estimating");
  if (progress.model_progress?.length) {
    if (completed >= total) return copy(locale, "almostDone");
    const remaining = progress.model_progress.filter((model) => model.succeeded + model.failed < model.total);
    if (!remaining.length) return copy(locale, "almostDone");
    if (remaining.some((model) => model.retrying)) return locale === "zh-CN" ? "等待模型重试" : "Waiting for model retry";
    return progress.model_eta_seconds != null ? formatRemaining(progress.model_eta_seconds, locale) : copy(locale, "estimating");
  }
  if (completed <= 0) return copy(locale, "waitingFirst");
  if (completed >= total) return copy(locale, "almostDone");
  // One fast result is not a representative rate for a parallel batch. A
  // stalled batch also cannot support an increasingly large time promise.
  if (completed < Math.min(3, total)) return copy(locale, "estimating");
  const lastCompletion = [...progress.messages].reverse().find((event) => event.message === "Completed one grading unit.");
  const lastCompletedAt = normalizeTimestamp(lastCompletion?.ts);
  if (!lastCompletedAt || Date.now() - lastCompletedAt > 60_000) return copy(locale, "estimating");
  const startedAt = normalizeTimestamp(progress.started_at ?? progress.messages[0]?.ts);
  if (!startedAt) return copy(locale, "estimating");
  const elapsedSeconds = Math.max(1, (Date.now() - startedAt) / 1000);
  const rate = completed / elapsedSeconds;
  if (!Number.isFinite(rate) || rate <= 0) return copy(locale, "estimating");
  const remainingSeconds = Math.max(1, Math.round((total - completed) / rate));
  return formatRemaining(remainingSeconds, locale);
}

function formatRemaining(remainingSeconds: number, locale: Locale) {
  if (remainingSeconds < 60) {
    return copy(locale, "aboutSeconds").replace("{value}", String(Math.max(5, Math.ceil(remainingSeconds / 5) * 5)));
  }
  const minutes = Math.ceil(remainingSeconds / 60);
  if (minutes < 60) return copy(locale, "aboutMinutes").replace("{value}", String(minutes));
  const hours = Math.max(1, Math.ceil(minutes / 60));
  return copy(locale, "aboutHours").replace("{value}", String(hours));
}

function normalizeTimestamp(value?: number | null): number | null {
  if (!value || !Number.isFinite(value)) return null;
  return value > 1_000_000_000_000 ? value : value * 1000;
}

function QueueTable({ locale, queue, completed }: { locale: Locale; queue: QueueSummary; completed: boolean }) {
  const rows = [
    { label: "completed" as const, count: queue.completed, state: "ok" as const, detail: completed ? "draftWritten" as const : "processingFinished" as const, tone: "success" as const },
    { label: "running" as const, count: queue.running, state: "grading" as const, detail: "expertsWorking" as const, tone: "primary" as const },
    { label: "queued" as const, count: queue.queued, state: "queuedState" as const, detail: "taskQueue" as const, tone: "neutral" as const },
    { label: "errorSignals" as const, count: queue.errorSignals, state: queue.errorSignals > 0 ? "errorSignals" as const : "none" as const, detail: queue.errorSignals > 0 ? "recentErrors" as const : "noErrors" as const, tone: queue.errorSignals > 0 ? "danger" as const : "success" as const },
  ];
  return (
    <section className="mt-10 overflow-hidden rounded-[8px] bg-card ring-1 ring-inset ring-border" aria-label={copy(locale, "queue")}>
      <div className="hidden h-[42px] grid-cols-[220px_120px_140px_minmax(0,1fr)] items-center bg-slate-50 px-[14px] text-[12px] font-semibold text-muted-foreground dark:bg-slate-900/40 sm:grid">
        <span>{copy(locale, "queue")}</span>
        <span>{copy(locale, "count")}</span>
        <span>{copy(locale, "state")}</span>
        <span>{copy(locale, "description")}</span>
      </div>
      <div className="divide-y">
        {rows.map((row) => (
          <div key={row.label} className="grid min-h-[48px] gap-2 px-4 py-3 text-[13px] sm:grid-cols-[220px_120px_140px_minmax(0,1fr)] sm:items-center sm:gap-0 sm:px-[14px] sm:py-0">
            <span className="font-semibold text-foreground">{copy(locale, row.label)}</span>
            <span className="text-muted-foreground">{row.count}</span>
            <span className={cn(
              "inline-flex min-h-7 w-fit min-w-[84px] items-center justify-center rounded-full px-3 text-[11px] font-semibold sm:text-[12px]",
              row.tone === "success" && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300",
              row.tone === "primary" && "bg-blue-100 text-primary dark:bg-blue-950/60",
              row.tone === "neutral" && "bg-slate-100 text-muted-foreground dark:bg-slate-800",
              row.tone === "danger" && "bg-red-100 text-danger dark:bg-red-950/40",
            )}>{copy(locale, row.state)}</span>
            <span className="min-w-0 text-muted-foreground">{copy(locale, row.detail)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function PageState({
  title,
  description,
  busy = false,
  action,
  href,
  onAction,
  secondaryAction,
  secondaryHref,
}: {
  title: string;
  description?: string;
  busy?: boolean;
  action?: string;
  href?: string;
  onAction?: () => void;
  secondaryAction?: string;
  secondaryHref?: string;
}) {
  return (
    <section className="mx-auto mt-[25px] flex min-h-[360px] w-full max-w-[940px] flex-col items-center justify-center rounded-[10px] border bg-card px-6 text-center" aria-busy={busy || undefined}>
      {busy ? <><SmarTAIMascot variant="grading" size="lg" /><LoaderCircle aria-hidden="true" className="mb-4 h-7 w-7 animate-spin text-primary" /></> : <CheckCircle2 aria-hidden="true" className="mb-4 h-7 w-7 text-muted-foreground" />}
      <h2 className="text-base font-semibold text-foreground">{title}</h2>
      {description ? <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{description}</p> : null}
      <div className="mt-5 flex flex-col gap-2 sm:flex-row">
        {action && href ? <Link to={href} className="inline-flex h-9 items-center justify-center rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground">{action}</Link> : null}
        {action && onAction ? <button type="button" onClick={onAction} className="h-9 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground">{action}</button> : null}
        {secondaryAction && secondaryHref ? <Link to={secondaryHref} className="inline-flex h-9 items-center justify-center rounded-[7px] border bg-card px-4 text-sm font-semibold hover:bg-muted">{secondaryAction}</Link> : null}
      </div>
    </section>
  );
}
