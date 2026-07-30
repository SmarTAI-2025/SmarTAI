import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Loader2,
  RefreshCw,
} from "lucide-react";
import type { ReactNode } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useTask } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { Button } from "@/components/ui/Button";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { getTaskDestination } from "@/lib/taskFlow";
import type { JobProgress, ProgressEvent, TaskStatus } from "@/types";

type RecognitionStepState = "done" | "active" | "pending";

interface RecognitionStep {
  key: MessageKey;
  state: RecognitionStepState;
}

const FINISHED_SUBMISSION_STATUSES = new Set<TaskStatus>(["submissions_ready"]);

/** Figma 05 visual language adapted to the S02 submission-recognition facts. */
export function SubmissionRecognitionProgressPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const { locale, t } = useI18n();
  const taskQuery = useTask(taskId);
  const progressQuery = useTaskProgress(taskId);
  const status = (progressQuery.data?.status ?? taskQuery.data?.status) as TaskStatus | undefined;

  if (taskId && status === "draft") {
    return <Navigate to={`/tasks/${taskId}/upload/problems`} replace />;
  }
  if (taskId && status === "extracting_problems") {
    return <Navigate to={`/tasks/${taskId}/problems/progress`} replace />;
  }
  if (taskId && status === "problems_ready") {
    return <Navigate to={`/tasks/${taskId}/submissions/upload`} replace />;
  }
  if (taskId && status === "grading") {
    return <Navigate to={`/tasks/${taskId}/grading/progress`} replace />;
  }
  if (taskId && status === "graded") {
    return <Navigate to={`/tasks/${taskId}/review`} replace />;
  }
  if (taskId && taskQuery.data && ["review_confirmed", "generating_analysis", "finalized"].includes(status ?? "")) {
    return <Navigate to={getTaskDestination(taskQuery.data)} replace />;
  }
  if (taskId && status && FINISHED_SUBMISSION_STATUSES.has(status)) {
    return <Navigate to={`/tasks/${taskId}/submissions`} replace />;
  }

  const refresh = () => {
    void Promise.all([taskQuery.refetch(), progressQuery.refetch()]);
  };
  const progressFailure = progressQuery.progress?.error_detail
    ?? [...(progressQuery.progress?.messages ?? [])].reverse().find((event) => event.level === "error")?.message
    ?? taskQuery.data?.error
    ?? progressQuery.error
    ?? taskQuery.error;

  if (!taskId) {
    return (
      <ProgressPageFrame title={t("submissionProgressTitle")}>
        <RecoveryState
          title={t("submissionProgressTaskMissing")}
          description={t("submissionProgressTaskMissingDescription")}
          primaryLabel={t("submissionProgressViewTasks")}
          onPrimary={() => navigate("/history")}
        />
      </ProgressPageFrame>
    );
  }

  if (status === "error") {
    const info = classifyRecoverableError(progressFailure, {
      locale,
      phase: progressQuery.progress?.current_step ?? progressQuery.progress?.phase ?? "submission_recognition",
      jobId: taskQuery.data?.last_failed_job_id,
      returnTo: `/tasks/${taskId}/submissions/progress`,
    });
    return (
      <ProgressPageFrame title={t("submissionProgressTitle")}>
        <RecoverableActionState
          info={info}
          locale={locale}
          className="min-h-[430px]"
          primaryAction={info.actionKind === "byok" ? undefined : {
            label: info.actionKind === "refresh" ? info.actionLabel : t("submissionProgressChooseAgain"),
            onClick: info.actionKind === "refresh" ? refresh : () => navigate(`/tasks/${taskId}/submissions/upload`),
            busy: taskQuery.isFetching || progressQuery.isFetching,
          }}
          secondaryAction={{
            label: t("submissionProgressRefresh"),
            onClick: refresh,
            busy: taskQuery.isFetching || progressQuery.isFetching,
          }}
        />
      </ProgressPageFrame>
    );
  }

  const hasReadableState = Boolean(status || taskQuery.data || progressQuery.data);
  if (!hasReadableState && (taskQuery.isError || progressQuery.isError)) {
    const info = classifyRecoverableError(progressQuery.error ?? taskQuery.error, {
      locale,
      phase: "submission_recognition_status",
      returnTo: `/tasks/${taskId}/submissions/progress`,
    });
    return (
      <ProgressPageFrame title={t("submissionProgressTitle")}>
        <RecoverableActionState
          info={info}
          locale={locale}
          className="min-h-[430px]"
          primaryAction={info.actionKind === "byok" ? undefined : {
            label: t("submissionProgressRefresh"),
            onClick: refresh,
            busy: taskQuery.isFetching || progressQuery.isFetching,
          }}
          secondaryAction={{ label: t("submissionProgressViewTasks"), href: "/history" }}
        />
      </ProgressPageFrame>
    );
  }

  if (status !== "parsing_submissions") {
    return (
      <ProgressPageFrame title={t("submissionProgressTitle")}>
        <LoadingState label={t("submissionProgressReading")} />
      </ProgressPageFrame>
    );
  }

  const progress = progressQuery.progress;
  const metrics = progress?.stage_metrics ?? {};
  const totalFiles = metric(metrics, "files_total", progress?.total_students ?? 0);
  const processedFiles = metric(metrics, "files_processed", progress?.completed_units ?? 0);
  const matchedIdentities = metric(metrics, "identities_matched");
  const reviewIdentities = metric(metrics, "identities_needing_review");
  const answersSplit = metric(metrics, "answers_split");
  const hasDeterminateProgress = totalFiles > 0;
  const percent = hasDeterminateProgress ? progressQuery.percent : null;
  const recentEvents = (progress?.messages ?? []).slice(-3);

  return (
    <ProgressPageFrame title={t("submissionProgressTitle")}>
      <section
        className="flex min-h-[430px] w-full flex-col rounded-[10px] border bg-card px-5 py-7 sm:px-10 sm:py-10"
        aria-live="polite"
        aria-busy="true"
      >
        <div>
          <h2 className="text-[22px] font-bold leading-8 tracking-[-0.01em] text-foreground sm:text-2xl">
            {t("submissionProgressRecognizing")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {hasDeterminateProgress
              ? `${t("submissionProgressProcessed")} ${processedFiles} / ${totalFiles} ${t("submissionProgressFiles")} · ${t("submissionProgressBackgroundSuffix")}`
              : t("submissionProgressBackgroundDescription")}
          </p>
        </div>

        <div className="mt-6 flex items-center gap-3">
          <div
            className="relative h-2 flex-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
            role="progressbar"
            aria-label={t("submissionProgressProgressLabel")}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent ?? undefined}
            aria-valuetext={percent === null ? t("submissionProgressProcessing") : undefined}
          >
            {percent === null ? (
              <span className="absolute inset-y-0 left-0 w-1/3 animate-pulse rounded-full bg-primary" />
            ) : (
              <span
                className="block h-full rounded-full bg-primary transition-[width] duration-300"
                style={{ width: `${percent}%` }}
              />
            )}
          </div>
          <span className="w-14 shrink-0 text-right text-sm font-semibold text-primary sm:text-lg">
            {percent === null ? t("submissionProgressProcessing") : `${percent}%`}
          </span>
        </div>

        <div className="mt-8 grid min-h-0 flex-1 gap-6 md:grid-cols-[210px_minmax(0,1fr)] md:gap-16">
          <ol className="grid content-start gap-3" aria-label={t("submissionProgressStepsLabel")}>
            {getRecognitionSteps(progress).map((step) => (
              <RecognitionStepItem key={step.key} step={step} label={t(step.key)} />
            ))}
          </ol>

          <div className="min-w-0 rounded-lg border bg-slate-50 px-5 py-4 dark:bg-slate-900/30">
            <h3 className="text-sm font-semibold text-foreground">
              {t("submissionProgressLatestEvents")}
            </h3>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">
              {t("submissionProgressMatched")} {matchedIdentities}
              {" · "}{t("submissionProgressNeedsReview")} {reviewIdentities}
              {" · "}{t("submissionProgressAnswersSplit")} {answersSplit}
            </p>
            {recentEvents.length > 0 ? (
              <ol className="mt-2 grid gap-1">
                {recentEvents.map((event, index) => (
                  <li
                    key={`${event.ts}:${event.message}:${index}`}
                    className="grid min-w-0 grid-cols-[42px_minmax(0,1fr)] gap-2 text-xs leading-5 text-muted-foreground"
                  >
                    <time dateTime={toDateTime(event.ts)}>{formatEventTime(event.ts, locale)}</time>
                    <span className="min-w-0 break-words">{localizeEvent(event, t)}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin text-primary" />
                <span>{t("submissionProgressWaitingForEvents")}</span>
              </div>
            )}
          </div>
        </div>
      </section>

      <div className="mt-[30px] flex flex-col justify-end gap-3 sm:flex-row">
        <Button
          type="button"
          variant="secondary"
          className="h-10 w-full px-6 sm:w-auto sm:min-w-[130px]"
          onClick={() => navigate("/")}
        >
          {t("submissionProgressRunInBackground")}
        </Button>
        <Button
          type="button"
          variant="secondary"
          className="h-10 w-full px-6 sm:w-auto sm:min-w-[150px]"
          onClick={() => navigate("/history")}
        >
          {t("submissionProgressViewTasks")}
        </Button>
      </div>
    </ProgressPageFrame>
  );
}

function ProgressPageFrame({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {title}
      </h1>
      <NewTaskStepper currentStep={3} />
      <div className="mx-auto mt-[45px] w-full max-w-[800px]">{children}</div>
    </div>
  );
}

function RecognitionStepItem({ step, label }: { step: RecognitionStep; label: string }) {
  return (
    <li
      className={cn(
        "flex min-h-7 items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold sm:text-sm",
        step.state === "done"
          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/70 dark:text-emerald-200"
          : step.state === "active"
            ? "bg-blue-100 text-primary dark:bg-blue-950/70 dark:text-blue-200"
            : "bg-slate-100 text-muted-foreground dark:bg-slate-800",
      )}
    >
      {step.state === "done" ? (
        <CheckCircle2 aria-hidden="true" className="h-4 w-4 shrink-0" />
      ) : step.state === "active" ? (
        <Loader2 aria-hidden="true" className="h-4 w-4 shrink-0 animate-spin" />
      ) : (
        <Clock3 aria-hidden="true" className="h-4 w-4 shrink-0" />
      )}
      <span className="min-w-0 break-words">{label}</span>
    </li>
  );
}

function LoadingState({ label }: { label: string }) {
  return (
    <section className="flex min-h-[430px] items-center justify-center rounded-[10px] border bg-card px-6 text-center">
      <div>
        <Loader2 aria-hidden="true" className="mx-auto h-7 w-7 animate-spin text-primary" />
        <p className="mt-4 text-sm font-medium text-muted-foreground">{label}</p>
      </div>
    </section>
  );
}

function RecoveryState({
  title,
  description,
  primaryLabel,
  onPrimary,
  secondaryLabel,
  onSecondary,
  busy = false,
}: {
  title: string;
  description: string;
  primaryLabel: string;
  onPrimary: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
  busy?: boolean;
}) {
  return (
    <section className="flex min-h-[430px] items-center justify-center rounded-[10px] border bg-card px-6 py-10 text-center">
      <div className="max-w-md">
        <span className="mx-auto inline-flex h-11 w-11 items-center justify-center rounded-full bg-red-50 text-danger dark:bg-red-950/40">
          <AlertCircle aria-hidden="true" className="h-5 w-5" />
        </span>
        <h2 className="mt-4 text-xl font-bold text-foreground">{title}</h2>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">{description}</p>
        <div className="mt-6 flex flex-col justify-center gap-3 sm:flex-row">
          <Button type="button" className="h-10 px-5" onClick={onPrimary} disabled={busy}>
            {busy ? <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
            {primaryLabel}
          </Button>
          {secondaryLabel && onSecondary ? (
            <Button
              type="button"
              variant="secondary"
              className="h-10 px-5"
              onClick={onSecondary}
              disabled={busy}
            >
              {secondaryLabel === primaryLabel && busy ? (
                <RefreshCw aria-hidden="true" className="h-4 w-4 animate-spin" />
              ) : null}
              {secondaryLabel}
            </Button>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function getRecognitionSteps(progress: JobProgress | null): RecognitionStep[] {
  const currentStep = progress?.current_step;
  const finished = progress?.phase === "done" || currentStep === "completed";
  const consolidating = currentStep === "consolidating_submission_results";
  const recognizing = currentStep === "recognizing_submissions" || (progress?.completed_units ?? 0) > 0;
  const preparing = currentStep === "preparing_submission_files" || !currentStep;

  return [
    { key: "submissionProgressStepFilesReceived", state: "done" },
    {
      key: "submissionProgressStepPrepareFiles",
      state: finished || consolidating || recognizing ? "done" : preparing ? "active" : "pending",
    },
    {
      key: "submissionProgressStepIdentityMatching",
      state: finished || consolidating ? "done" : recognizing ? "active" : "pending",
    },
    {
      key: "submissionProgressStepAnswerSplitting",
      state: finished || consolidating ? "done" : recognizing ? "active" : "pending",
    },
    {
      key: "submissionProgressStepSaveResults",
      state: finished ? "done" : consolidating ? "active" : "pending",
    },
  ];
}

function localizeEvent(event: ProgressEvent, t: (key: MessageKey) => string): string {
  const message = event.message.toLowerCase();
  if (message === "phase: parsing") return t("submissionProgressEventParsing");
  if (message === "phase: done") return t("submissionProgressEventDone");
  if (message.startsWith("reading ") && message.endsWith("...")) {
    return t("submissionProgressEventReadingFile");
  }
  if (message.startsWith("detected scanned pdf:") && message.includes("rendering pages for ocr")) {
    return t("submissionProgressEventScannedPdf");
  }
  if (message.startsWith("ocr recognizing ")) {
    return t("submissionProgressEventOcrRecognizing");
  }
  if (message.startsWith("ocr warning for ")) {
    return t("submissionProgressEventOcrWarning");
  }
  if (message === "submission files prepared.") return t("submissionProgressEventFilesPrepared");
  if (message === "submission recognition started.") return t("submissionProgressEventStarted");
  if (message === "submission recognized.") return t("submissionProgressEventRecognized");
  if (message === "consolidating recognized submissions.") return t("submissionProgressEventConsolidating");
  if (message === "submission recognition completed.") return t("submissionProgressEventDone");
  if (event.level === "warn") return t("submissionProgressFailedDescription");
  return event.message;
}

function metric(metrics: Record<string, number>, key: string, fallback = 0) {
  const value = metrics[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : fallback;
}

function formatEventTime(timestamp: number, locale: string): string {
  const date = normalizeTimestamp(timestamp);
  if (!date) return "—";
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function toDateTime(timestamp: number): string | undefined {
  return normalizeTimestamp(timestamp)?.toISOString();
}

function normalizeTimestamp(timestamp: number): Date | null {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return null;
  return new Date(timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1_000);
}
