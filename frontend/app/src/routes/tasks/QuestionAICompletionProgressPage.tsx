import { CheckCircle2, Clock3, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { useAICompletionJob } from "@/api/hooks";
import { taskKeys } from "@/api/hooks/keys";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { aiCompletionText } from "@/lib/aiCompletionCopy";
import { cn } from "@/lib/cn";
import type { ProgressEvent } from "@/types";

const STEPS = ["stepScope", "stepGenerate", "stepValidate", "stepSave"] as const;

export function QuestionAICompletionProgressPage() {
  const { taskId, jobId } = useParams();
  const { locale } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const completionQuery = useAICompletionJob(taskId, jobId);
  const redirectingRef = useRef(false);
  const job = completionQuery.data;

  useEffect(() => {
    if (!taskId || job?.status !== "done" || redirectingRef.current) return;
    redirectingRef.current = true;
    void queryClient.invalidateQueries({ queryKey: taskKeys.all }).finally(() => {
      navigate(`/tasks/${taskId}/questions`, { replace: true });
    });
  }, [job?.status, navigate, queryClient, taskId]);

  if (!taskId || !jobId) {
    return (
      <ProgressFrame locale={locale}>
        <RecoveryState
          locale={locale}
          title={aiCompletionText(locale, "jobMissing")}
          description={aiCompletionText(locale, "progressReadFailedDescription")}
          taskId={taskId}
        />
      </ProgressFrame>
    );
  }

  const loadError = completionQuery.error ? normalizeAPIError(completionQuery.error) : null;
  const expired = loadError?.status === 410 || getAPIErrorCode(loadError) === "ai_completion_job_expired";
  const failed = job?.status === "error";
  if (failed || completionQuery.isError) {
    return (
      <ProgressFrame locale={locale}>
        <RecoveryState
          locale={locale}
          title={aiCompletionText(locale, failed ? "progressFailedTitle" : "progressReadFailedTitle")}
          description={aiCompletionText(locale, expired || failed ? "progressFailedDescription" : "progressReadFailedDescription")}
          taskId={taskId}
          onRefresh={expired ? undefined : () => void completionQuery.refetch()}
          refreshing={completionQuery.isFetching}
        />
      </ProgressFrame>
    );
  }

  if (!job || job.status === "done") {
    return (
      <ProgressFrame locale={locale}>
        <section className="flex min-h-[430px] items-center justify-center rounded-[10px] border bg-card px-6 text-center" aria-live="polite">
          <div>
            <Loader2 aria-hidden="true" className="mx-auto h-7 w-7 animate-spin text-primary" />
            <p className="mt-3 text-sm font-semibold text-foreground">{aiCompletionText(locale, "processing")}</p>
          </div>
        </section>
      </ProgressFrame>
    );
  }

  const progress = job.progress;
  const hasDeterminateProgress = Boolean(
    progress
    && typeof progress.total_steps === "number"
    && progress.total_steps > 0
    && typeof progress.completed_steps === "number",
  );
  const percent = hasDeterminateProgress
    ? Math.max(0, Math.min(100, Math.round(((progress?.completed_steps ?? 0) / (progress?.total_steps ?? 1)) * 100)))
    : null;
  const completedSteps = typeof progress?.completed_steps === "number" ? progress.completed_steps : 0;
  const recentEvents = [...(progress?.messages ?? [])].slice(-3).reverse();

  return (
    <ProgressFrame locale={locale}>
      <section className="flex min-h-[430px] w-full min-w-0 flex-col rounded-[10px] border bg-card px-5 py-7 sm:px-10 sm:py-10" aria-live="polite" aria-busy="true">
        <header>
          <h2 className="text-[22px] font-bold leading-8 tracking-[-0.01em] text-foreground sm:text-2xl">{aiCompletionText(locale, "progressHeading")}</h2>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">{aiCompletionText(locale, "progressBackground")}</p>
        </header>

        <div className="mt-6 flex min-w-0 items-center gap-3">
          <div className="relative h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700" role="progressbar" aria-label={aiCompletionText(locale, "progressLabel")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined} aria-valuetext={percent === null ? aiCompletionText(locale, "processing") : undefined}>
            {percent === null ? <span className="absolute inset-y-0 left-0 w-1/3 animate-pulse rounded-full bg-primary" /> : <span className="block h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: `${percent}%` }} />}
          </div>
          <span className="w-14 shrink-0 text-right text-sm font-semibold text-primary sm:text-lg">{percent === null ? aiCompletionText(locale, "processing") : `${percent}%`}</span>
        </div>

        <div className="mt-8 grid min-h-0 flex-1 gap-6 md:grid-cols-[210px_minmax(0,1fr)] md:gap-16">
          <section aria-labelledby="ai-completion-steps-title">
            <h3 id="ai-completion-steps-title" className="sr-only">{aiCompletionText(locale, "stepsTitle")}</h3>
            <ol className="grid content-start gap-3">
              {STEPS.map((key, index) => {
                const state = completionStepState(index, completedSteps);
                return <ProgressStep key={key} label={aiCompletionText(locale, key)} state={state} />;
              })}
            </ol>
          </section>

          <section className="min-w-0 rounded-lg border bg-slate-50 px-5 py-4 dark:bg-slate-900/30" aria-labelledby="ai-completion-events-title">
            <h3 id="ai-completion-events-title" className="text-sm font-semibold text-foreground">{aiCompletionText(locale, "latestEvents")}</h3>
            {recentEvents.length > 0 ? (
              <ol className="mt-3 grid gap-1.5">
                {recentEvents.map((event, index) => (
                  <li key={`${event.ts}:${event.message}:${index}`} className="grid min-w-0 grid-cols-[42px_minmax(0,1fr)] gap-2 text-xs leading-5 text-muted-foreground sm:text-sm">
                    <time dateTime={toDateTime(event.ts)}>{formatEventTime(event.ts, locale)}</time>
                    <span className="min-w-0 break-words">{localizeEvent(event, locale)}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="mt-4 flex items-center gap-2 text-sm text-muted-foreground"><Loader2 aria-hidden="true" className="h-4 w-4 animate-spin text-primary" /><span>{aiCompletionText(locale, "waitingEvents")}</span></div>
            )}
          </section>
        </div>
      </section>

      <div className="mt-[30px] flex flex-col justify-end gap-3 sm:flex-row">
        <Link to="/" className="inline-flex h-10 w-full items-center justify-center rounded-[8px] border bg-card px-6 text-sm font-semibold text-foreground hover:bg-muted sm:w-auto sm:min-w-[130px]">{aiCompletionText(locale, "runInBackground")}</Link>
        <Link to="/history" className="inline-flex h-10 w-full items-center justify-center rounded-[8px] border bg-card px-6 text-sm font-semibold text-foreground hover:bg-muted sm:w-auto sm:min-w-[150px]">{aiCompletionText(locale, "viewTasks")}</Link>
      </div>
    </ProgressFrame>
  );
}

function ProgressFrame({ locale, children }: { locale: Locale; children: ReactNode }) {
  return (
    <div className="min-w-0 w-full max-w-[1300px]">
      <h1 className="break-words text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">{aiCompletionText(locale, "progressTitle")}</h1>
      <NewTaskStepper currentStep={2} />
      <div className="mx-auto mt-[45px] w-full max-w-[800px]">{children}</div>
    </div>
  );
}

function RecoveryState({ locale, title, description, taskId, onRefresh, refreshing = false }: { locale: Locale; title: string; description: string; taskId?: string; onRefresh?: () => void; refreshing?: boolean }) {
  return (
    <section className="flex min-h-[430px] flex-col items-center justify-center rounded-[10px] border bg-card px-6 text-center">
      <h2 className="text-xl font-bold text-foreground">{title}</h2>
      <p className="mt-2 max-w-lg text-sm leading-6 text-muted-foreground">{description}</p>
      <div className="mt-6 flex w-full max-w-lg flex-col justify-center gap-3 sm:flex-row">
        <Link to={`/tasks/${taskId ?? ""}/questions/ai-complete`} className="inline-flex h-10 items-center justify-center rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground hover:opacity-90">{aiCompletionText(locale, "chooseAgain")}</Link>
        <Link to="/settings/byok" className="inline-flex h-10 items-center justify-center rounded-[8px] border bg-card px-5 text-sm font-semibold text-foreground hover:bg-muted">{aiCompletionText(locale, "configureModels")}</Link>
        {onRefresh ? <button type="button" disabled={refreshing} onClick={onRefresh} className="inline-flex h-10 items-center justify-center gap-2 rounded-[8px] border bg-card px-5 text-sm font-semibold text-foreground hover:bg-muted disabled:opacity-50"><RefreshCw aria-hidden="true" className={cn("h-4 w-4", refreshing && "animate-spin")} />{aiCompletionText(locale, "refresh")}</button> : null}
      </div>
    </section>
  );
}

function ProgressStep({ label, state }: { label: string; state: "done" | "active" | "pending" }) {
  return (
    <li className={cn("flex min-h-7 items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold sm:text-sm", state === "done" ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/70 dark:text-emerald-200" : state === "active" ? "bg-blue-100 text-primary dark:bg-blue-950/70 dark:text-blue-200" : "bg-slate-100 text-muted-foreground dark:bg-slate-800")}>
      {state === "done" ? <CheckCircle2 aria-hidden="true" className="h-4 w-4 shrink-0" /> : state === "active" ? <Loader2 aria-hidden="true" className="h-4 w-4 shrink-0 animate-spin" /> : <Clock3 aria-hidden="true" className="h-4 w-4 shrink-0" />}
      <span className="min-w-0 break-words">{label}</span>
    </li>
  );
}

function completionStepState(index: number, completedSteps: number): "done" | "active" | "pending" {
  if (index === 0) return "done";
  const workIndex = index - 1;
  if (completedSteps > workIndex) return "done";
  if (completedSteps === workIndex) return "active";
  return "pending";
}

function localizeEvent(event: ProgressEvent, locale: Locale) {
  if (locale === "en-US") return event.message;
  const known: Record<string, string> = {
    "AI completion started": "SmarTAI 补全已开始",
    "Generating missing problem materials": "正在生成缺失的题目资料",
    "Validating generated problem materials": "正在校验生成的题目资料",
    "Applying generated materials to still-empty slots": "正在写回仍为空的资料槽位",
    "AI completion finished": "SmarTAI 补全已完成",
  };
  return known[event.message] ?? event.message;
}

function formatEventTime(value: number, locale: Locale) {
  const milliseconds = value > 10_000_000_000 ? value : value * 1000;
  if (!Number.isFinite(milliseconds)) return "—";
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(milliseconds));
}

function toDateTime(value: number) {
  const milliseconds = value > 10_000_000_000 ? value : value * 1000;
  return Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString() : "";
}
