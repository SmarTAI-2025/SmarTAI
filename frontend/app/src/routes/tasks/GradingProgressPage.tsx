import { AlertTriangle, CheckCircle2, LoaderCircle } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { useStartGrading, useTask } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { gradingProgressText as copy } from "@/lib/gradingProgressCopy";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import {
  getTaskDestination,
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
  const eta = useMemo(() => estimateRemaining(progress, queue.total, queue.completed, locale), [locale, progress, queue.completed, queue.total]);
  const latestError = progress?.error_detail
    ?? [...(progress?.messages ?? [])].reverse().find((event) => event.level === "error")?.message
    ?? state?.error
    ?? task?.error
    ?? null;

  if (taskId && status === "submissions_ready") {
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
    void Promise.all([taskQuery.refetch(), progressQuery.refetch()]);
  };

  async function handleRetry() {
    if (!taskId) return;
    try {
      const response = await retryGrading.mutateAsync({ taskId });
      if (response.status === "already_done") {
        navigate(`/tasks/${taskId}/review`, { replace: true });
        return;
      }
      await Promise.all([taskQuery.refetch(), progressQuery.refetch()]);
    } catch {
      // Error is rendered in the recovery strip without losing task context.
    }
  }

  const hasReadableState = Boolean(status || task || state);
  const readFailed = !hasReadableState && (taskQuery.isError || progressQuery.isError);
  const isFinalizing = status === "grading" && progress?.phase === "done";
  const percent = completedView ? 100 : progress ? progressQuery.percent : null;
  const recoveryInfo = status === "error"
    ? classifyRecoverableError(retryGrading.error ?? latestError, {
      locale,
      phase: progress?.current_step ?? progress?.phase ?? "grading",
      jobId: task?.last_failed_job_id ?? task?.grading_job_id,
      returnTo: `/tasks/${taskId}/grading/progress`,
    })
    : null;

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {copy(locale, "title")}
      </h1>
      <NewTaskStepper currentStep={5} />

      {!taskId ? (
        <PageState
          title={copy(locale, "missingTask")}
          action={copy(locale, "viewHistory")}
          href="/history"
        />
      ) : readFailed ? (
        <PageState
          title={copy(locale, "readError")}
          description={copy(locale, "readErrorDescription")}
          action={copy(locale, "refresh")}
          onAction={refresh}
          secondaryAction={copy(locale, "viewHistory")}
          secondaryHref="/history"
        />
      ) : !status ? (
        <PageState title={copy(locale, "reading")} busy />
      ) : (
        <div className="mx-auto mt-[25px] w-full max-w-[940px]">
          {status === "error" && recoveryInfo ? (
            <RecoverableActionState
              info={recoveryInfo}
              locale={locale}
              className="min-h-[300px]"
              primaryAction={recoveryInfo.actionKind === "byok" ? undefined : {
                label: recoveryInfo.actionKind === "refresh" ? recoveryInfo.actionLabel : copy(locale, retryGrading.isPending ? "retrying" : "retry"),
                onClick: recoveryInfo.actionKind === "refresh" ? refresh : () => void handleRetry(),
                busy: retryGrading.isPending || taskQuery.isFetching || progressQuery.isFetching,
              }}
              secondaryAction={{
                label: copy(locale, "editExperts"),
                href: getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/progress`),
              }}
            />
          ) : (
            <>
              <section className="flex min-h-[220px] flex-col rounded-[10px] border bg-card px-5 pb-5 pt-7 sm:h-[220px] sm:px-10 sm:pb-5 sm:pt-8" aria-live="polite" aria-busy={completedView ? undefined : true}>
                <h2 className="text-[22px] font-bold leading-8 tracking-[-0.01em] text-foreground sm:text-[24px]">
                  {completedView
                    ? (locale === "en-US" ? "Grading Completed" : "批改已完成")
                    : copy(locale, isFinalizing ? "finalizing" : "gradingAnswers")}
                </h2>
                <p className="mt-1 text-[13px] leading-5 text-muted-foreground sm:text-[14px]">
                  {copy(locale, "completedPrefix")} {queue.completed} / {queue.total} {copy(locale, "unitSuffix")}
                  <span aria-hidden="true"> · </span>
                  {completedView
                    ? (locale === "en-US" ? "Historical progress snapshot" : "历史进度快照")
                    : `${copy(locale, "etaPrefix")} ${isFinalizing ? copy(locale, "almostDone") : eta}`}
                </p>

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
                      <span className="absolute inset-y-0 left-0 w-1/3 animate-pulse rounded-full bg-primary" />
                    ) : (
                      <span className="block h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: `${percent}%` }} />
                    )}
                  </div>
                  <span className="w-14 shrink-0 text-right text-[18px] font-bold leading-6 text-primary sm:text-[20px]">
                    {percent === null ? "—" : `${percent}%`}
                  </span>
                </div>

                <div className="mt-auto flex flex-col gap-3 pt-5 sm:flex-row sm:items-center sm:justify-between sm:pt-0">
                  <span className="inline-flex min-h-7 w-fit items-center rounded-full bg-teal-100 px-3 text-[11px] font-semibold text-teal-700 dark:bg-teal-950/60 dark:text-teal-300 sm:text-[12px]">
                    {completedView ? (locale === "en-US" ? "Completed" : "已完成") : copy(locale, "backgroundChip")}
                  </span>
                  <Link to={completedView ? `/tasks/${taskId}/review` : "/"} className="inline-flex h-10 w-full items-center justify-center rounded-[8px] border bg-card px-5 text-[13px] font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring sm:w-[150px] sm:text-[14px]">
                    {completedView ? (locale === "en-US" ? "View Grading Review" : "查看复核批改") : copy(locale, "backWorkspace")}
                  </Link>
                </div>
              </section>

              <QueueTable locale={locale} queue={queue} />

              <section className="mt-4 flex min-h-[74px] items-center rounded-[10px] border bg-transparent px-5 py-4 text-[13px] leading-5 text-muted-foreground sm:px-[30px] sm:text-[14px]">
                {latestError ? (
                  <span className="flex items-start gap-2 text-danger">
                    <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                    {latestError}
                  </span>
                ) : completedView
                  ? (locale === "en-US" ? "This page preserves the completed grading queue for later review." : "这里保留已完成批改的队列快照，便于之后回看。")
                  : copy(locale, "recoverableHint")}
              </section>
            </>
          )}
        </div>
      )}
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
  const running = Math.min(Math.max(0, total - completed), activePairs.size);
  const queued = Math.max(0, total - completed - running);
  const eventErrors = (progress?.messages ?? []).filter((event) => event.level === "error").length;
  const errorSignals = eventErrors + (progress?.error_detail && eventErrors === 0 ? 1 : 0);
  return { total, completed, running, queued, errorSignals };
}

function estimateRemaining(progress: JobProgress | null, total: number, completed: number, locale: Locale): string {
  if (!progress || total <= 0) return copy(locale, "estimating");
  if (completed <= 0) return copy(locale, "waitingFirst");
  if (completed >= total) return copy(locale, "almostDone");
  const startedAt = normalizeTimestamp(progress.started_at ?? progress.messages[0]?.ts);
  if (!startedAt) return copy(locale, "estimating");
  const elapsedSeconds = Math.max(1, (Date.now() - startedAt) / 1000);
  const rate = completed / elapsedSeconds;
  if (!Number.isFinite(rate) || rate <= 0) return copy(locale, "estimating");
  const remainingSeconds = Math.max(1, Math.round((total - completed) / rate));
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

function QueueTable({ locale, queue }: { locale: Locale; queue: QueueSummary }) {
  const rows = [
    { label: "completed" as const, count: queue.completed, state: "ok" as const, detail: "draftWritten" as const, tone: "success" as const },
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
      {busy ? <LoaderCircle aria-hidden="true" className="mb-4 h-7 w-7 animate-spin text-primary" /> : <CheckCircle2 aria-hidden="true" className="mb-4 h-7 w-7 text-muted-foreground" />}
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
