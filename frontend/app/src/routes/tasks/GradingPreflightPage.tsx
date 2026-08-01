import { AlertTriangle, ArrowLeft, CheckCircle2, ChevronRight, LoaderCircle, Timer } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { normalizeAPIError } from "@/api/client";
import { useGradingSetup, useStartGrading, useTask } from "@/api/hooks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { gradingPreflightText as copy } from "@/lib/gradingPreflightCopy";
import { modelDisplayName } from "@/lib/modelPresentation";
import {
  canTaskBeRegraded,
  getTaskDestination,
  getTaskGradingSetupHref,
  hasTaskReachedStep,
} from "@/lib/taskFlow";
import type { GradingFeedbackLength, GradingSetup, ProblemInfo, StudentSubmission } from "@/types";

const AUTO_START_SECONDS = 10;

/** C02: one read-only checkpoint before the idempotent grading mutation. */
export function GradingPreflightPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
  const setupQuery = useGradingSetup(taskId);
  const startGrading = useStartGrading();
  const [countdown, setCountdown] = useState(AUTO_START_SECONDS);
  const [autoStartEnabled, setAutoStartEnabled] = useState(true);
  const startTriggeredRef = useRef(false);
  const startHandlerRef = useRef<() => void>(() => undefined);

  const task = taskQuery.data;
  const setupResponse = setupQuery.data;
  const setup = setupResponse?.grading_setup ?? null;
  const problems = useMemo(
    () => Object.values(task?.problem_data ?? {}).sort(compareProblems),
    [task?.problem_data],
  );
  const students = useMemo(
    () => Object.values(task?.student_data ?? {}),
    [task?.student_data],
  );
  const summary = useMemo(() => summarizeTask(problems, students), [problems, students]);
  const selectedExperts = useMemo(() => {
    if (!setup || !setupResponse) return [];
    const selected = new Set(setup.selected_provider_ids);
    return setupResponse.available_experts.filter((expert) => selected.has(expert.provider_id));
  }, [setup, setupResponse]);
  const riskItems = useMemo(
    () => buildRiskItems(summary, setupResponse?.readiness.warnings ?? [], locale),
    [locale, setupResponse?.readiness.warnings, summary],
  );
  const isRegrading = canTaskBeRegraded(task?.status);
  const historyView = Boolean(task && task.status !== "submissions_ready" && !isRegrading);

  const blockingIssues = setupResponse?.readiness.blocking_issues ?? [];
  const effectiveBlockingIssues = blockingIssues.filter(
    (issue) => !(isRegrading && issue === "invalid_state"),
  );
  const hasEnabledSelection = selectedExperts.length > 0 && selectedExperts.every((expert) => expert.enabled);
  const canStart = Boolean(
    taskId
    && (task?.status === "submissions_ready" || isRegrading)
    && setupResponse?.configured
    && setup
    && effectiveBlockingIssues.length === 0
    && hasEnabledSelection
    && summary.problemCount > 0
    && summary.studentCount > 0,
  );
  const countdownActive = canStart && autoStartEnabled && !startGrading.isPending;

  async function handleStart() {
    if (!taskId || !canStart || startTriggeredRef.current) return;
    startTriggeredRef.current = true;
    setAutoStartEnabled(false);
    try {
      const response = await startGrading.mutateAsync({ taskId });
      if (response.status === "already_done") {
        navigate(`/tasks/${taskId}/review`, { replace: true });
        return;
      }
      navigate(`/tasks/${taskId}/grading/progress`, { replace: true });
    } catch {
      // The normalized response is rendered below; task data remains intact.
      startTriggeredRef.current = false;
    }
  }

  startHandlerRef.current = () => { void handleStart(); };

  useEffect(() => {
    if (!canStart || historyView || !autoStartEnabled) {
      setCountdown(AUTO_START_SECONDS);
      return;
    }

    setCountdown(AUTO_START_SECONDS);
    const intervalId = window.setInterval(() => {
      setCountdown((current) => Math.max(0, current - 1));
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [autoStartEnabled, canStart, historyView, taskId]);

  useEffect(() => {
    if (countdownActive && countdown === 0) startHandlerRef.current();
  }, [countdown, countdownActive]);

  const isLoading = taskQuery.isLoading || setupQuery.isLoading;
  const isError = taskQuery.isError || setupQuery.isError;
  const startError = startGrading.error ? normalizeAPIError(startGrading.error).message : null;
  const disabledReason = historyView ? null : getDisabledReason({
    locale,
    configured: setupResponse?.configured ?? false,
    canStart,
    blockingIssues: effectiveBlockingIssues,
    hasEnabledSelection,
  });

  if (taskQuery.isSuccess && taskId && task) {
    if (task.status === "grading" || (task.status === "error" && task.grading_job_id)) {
      return <Navigate replace to={`/tasks/${taskId}/grading/progress`} />;
    }
    if (!hasTaskReachedStep(task, 5)) return <Navigate replace to={getTaskDestination(task)} />;
  }

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {copy(locale, "title")}
      </h1>
      <NewTaskStepper currentStep={5} />

      {isLoading ? (
        <PageState title={copy(locale, "loading")} busy />
      ) : isError ? (
        <PageState
          title={copy(locale, "loadError")}
          action={copy(locale, "retry")}
          onAction={() => { void taskQuery.refetch(); void setupQuery.refetch(); }}
        />
      ) : !taskId || !task ? (
        <PageState title={copy(locale, "missingTask")} href="/history" action={copy(locale, "retry")} />
      ) : (
        <div className="mx-auto mt-[34px] w-full max-w-[1100px]">
          {!historyView ? (
            <section className="overflow-hidden rounded-[10px] border border-primary/25 bg-card" aria-labelledby="grading-countdown-title" aria-live="polite">
              <div className="flex flex-col gap-5 px-6 py-5 sm:px-8 lg:flex-row lg:items-center">
                <div className="flex min-w-0 flex-1 items-center gap-4 sm:gap-5">
                  <span className={cn(
                    "inline-flex h-[70px] w-[70px] shrink-0 flex-col items-center justify-center rounded-full border-4 text-center",
                    countdownActive ? "border-primary/20 bg-primary/[0.055] text-primary" : "border-slate-200 bg-slate-50 text-muted-foreground dark:border-slate-700 dark:bg-slate-900/50",
                  )}>
                    <strong className="text-[25px] leading-7">{countdownActive ? countdown : "—"}</strong>
                    <span className="text-[10px] font-semibold">{locale === "en-US" ? "SEC" : "秒"}</span>
                  </span>
                  <div className="min-w-0">
                    <h2 id="grading-countdown-title" className="flex items-center gap-2 text-[18px] font-bold leading-6 text-foreground">
                      <Timer aria-hidden="true" className="h-5 w-5 text-primary" />
                      {copy(locale, isRegrading ? "regradeCountdownTitle" : "countdownTitle")}
                    </h2>
                    <p className="mt-1.5 text-[12px] leading-5 text-muted-foreground">
                      {countdownActive
                        ? copy(locale, isRegrading ? "regradeCountdownDescription" : "countdownDescription")
                        : disabledReason ?? copy(locale, isRegrading ? "regradeReadyMessage" : "readyMessage")}
                    </p>
                    {countdownActive ? <p className="mt-1 text-[11px] font-semibold text-primary">{countdown} {copy(locale, "countdownUnit")}</p> : null}
                  </div>
                </div>
                <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
                  <Link
                    to={getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/preflight`)}
                    className="inline-flex h-10 items-center justify-center gap-1.5 rounded-[8px] border bg-card px-4 text-[13px] font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <ArrowLeft aria-hidden="true" className="h-4 w-4" />
                    {copy(locale, "backToSetup")}
                  </Link>
                  <button
                    type="button"
                    disabled={!canStart || startGrading.isPending}
                    onClick={() => void handleStart()}
                    className="inline-flex h-10 items-center justify-center gap-1.5 rounded-[8px] bg-primary px-5 text-[13px] font-semibold text-primary-foreground outline-none hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {startGrading.isPending ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
                    {copy(locale, startGrading.isPending ? "starting" : isRegrading ? "regradeNow" : "startNow")}
                    {!startGrading.isPending ? <ChevronRight aria-hidden="true" className="h-4 w-4" /> : null}
                  </button>
                </div>
              </div>
              <div className="h-1 bg-slate-100 dark:bg-slate-800">
                <div
                  className="h-full bg-primary transition-[width] duration-1000 ease-linear"
                  style={{ width: countdownActive ? `${(countdown / AUTO_START_SECONDS) * 100}%` : "0%" }}
                />
              </div>
              {startError ? <p role="alert" className="border-t px-6 py-2 text-[11px] font-medium text-danger sm:px-8">{startError || copy(locale, "startError")}</p> : null}
            </section>
          ) : null}

          <section className={cn("min-h-[130px] rounded-[10px] border bg-card px-6 py-5 sm:px-8", historyView ? "" : "mt-[30px]")} aria-labelledby="preflight-task-summary">
            <h2 id="preflight-task-summary" className="text-[18px] font-bold leading-6 text-foreground">
              {copy(locale, "taskSummary")}
            </h2>
            <div className="mt-5 flex flex-wrap gap-3.5 sm:gap-5">
              <SummaryChip tone="primary" href={`/tasks/${taskId}/questions`}>
                {summary.problemCount} {locale === "zh-CN" ? copy(locale, "problems") : summary.problemCount === 1 ? "question" : "questions"}
              </SummaryChip>
              <SummaryChip tone="primary" href={`/tasks/${taskId}/submissions`}>
                {summary.studentCount} {locale === "zh-CN" ? copy(locale, "students") : summary.studentCount === 1 ? "student" : "students"}
              </SummaryChip>
              <SummaryChip
                tone={summary.criteriaComplete === summary.problemCount ? "success" : "warning"}
                href={questionOverviewHref(taskId, summary.criteriaComplete < summary.problemCount ? (locale === "en-US" ? "missing rubric" : "缺评分标准") : "")}
              >
                {copy(locale, "criteria")} {summary.criteriaComplete}/{summary.problemCount}
              </SummaryChip>
              <SummaryChip
                tone={summary.answersComplete === summary.problemCount ? "success" : "warning"}
                href={questionOverviewHref(taskId, summary.answersComplete < summary.problemCount ? (locale === "en-US" ? "missing reference answer" : "缺标答") : "")}
              >
                {copy(locale, "answers")} {summary.answersComplete}/{summary.problemCount}
              </SummaryChip>
              <SummaryChip
                tone={summary.programmingCount === 0 || summary.testsComplete === summary.programmingCount ? "success" : "warning"}
                href={questionOverviewHref(taskId, summary.testsComplete < summary.programmingCount ? (locale === "en-US" ? "programming" : "编程题") : "")}
              >
                {copy(locale, "tests")} {summary.programmingCount === 0 ? copy(locale, "notApplicable") : `${summary.testsComplete}/${summary.programmingCount}`}
              </SummaryChip>
            </div>
          </section>

          <section className="mt-[30px] min-h-[135px] rounded-[10px] border bg-card px-6 py-5 sm:px-8" aria-labelledby="preflight-experts">
            <div className="flex items-start justify-between gap-4">
              <h2 id="preflight-experts" className="text-[18px] font-bold leading-6 text-foreground">
                {copy(locale, "expertCombination")}
              </h2>
              {historyView ? (
                <span className="text-[11px] font-medium text-muted-foreground">
                  {locale === "en-US" ? "Historical configuration" : "历史配置快照"}
                </span>
              ) : (
                <Link to={getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/preflight`)} className="text-[12px] font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
                  {copy(locale, "editSetup")}
                </Link>
              )}
            </div>
            <div className="mt-5 flex flex-col gap-4 lg:flex-row lg:items-center">
              <div className="flex flex-wrap gap-3.5">
                {selectedExperts.map((expert) => (
                  <span key={expert.provider_id} className="inline-flex min-h-8 items-center rounded-full bg-blue-100 px-4 text-[13px] font-semibold text-primary dark:bg-blue-950/45">
                    {modelDisplayName(expert)}
                  </span>
                ))}
                {setup ? (
                  <span className="inline-flex min-h-8 items-center rounded-full bg-teal-100 px-4 text-[13px] font-semibold text-teal-700 dark:bg-teal-950/45 dark:text-teal-300">
                    {copy(locale, "aggregation")}: {aggregationLabel(setup.aggregation_method, locale)}
                  </span>
                ) : null}
              </div>
              <p className="min-w-0 text-[12px] leading-5 text-muted-foreground lg:ml-auto lg:max-w-[390px]">
                {copy(locale, "expertNote")}
              </p>
            </div>
          </section>

          <section className="mt-[30px] min-h-[135px] rounded-[10px] border bg-card px-6 py-5 sm:px-8" aria-labelledby="preflight-strategy">
            <div className="flex items-start justify-between gap-4">
              <h2 id="preflight-strategy" className="text-[18px] font-bold leading-6 text-foreground">
                {copy(locale, "scoringStrategy")}
              </h2>
              <span className="text-[11px] text-muted-foreground">{copy(locale, "exactTaskSetup")}</span>
            </div>
            {setup ? (
              <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(260px,1fr)_auto_auto_auto] lg:items-center lg:gap-6">
                <div className="flex items-center gap-4">
                  <span className="shrink-0 text-[13px] font-semibold text-foreground">{copy(locale, "strictness")}</span>
                  <span className="h-2 min-w-[150px] flex-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700" aria-label={`${copy(locale, "strictness")} ${setup.strictness}`}>
                    <span className="block h-full rounded-full bg-primary" style={{ width: `${setup.strictness}%` }} />
                  </span>
                  <span className="text-[12px] font-semibold text-primary">{setup.strictness}</span>
                </div>
                <StrategyChip tone={setup.allow_partial_credit ? "success" : "neutral"}>
                  {copy(locale, setup.allow_partial_credit ? "partialAllowed" : "partialDisabled")}
                </StrategyChip>
                <StrategyChip tone="warning">
                  {copy(locale, "confidence")} {setup.low_confidence_threshold.toFixed(2)}
                </StrategyChip>
                <StrategyChip tone="neutral">
                  {copy(locale, "feedback")}: {copy(locale, feedbackKey(setup.feedback_length))}
                </StrategyChip>
              </div>
            ) : (
              <p className="mt-5 text-sm text-danger">{copy(locale, "setupRequired")}</p>
            )}
          </section>

          <section className={cn(
            "mt-[30px] flex min-h-[62px] flex-col gap-3 rounded-[10px] border px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-8",
            riskItems.length > 0
              ? "border-amber-400 bg-amber-50 text-amber-950 dark:bg-amber-950/20 dark:text-amber-100"
              : "border-teal-200 bg-teal-50 text-teal-950 dark:border-teal-900 dark:bg-teal-950/20 dark:text-teal-100",
          )} aria-live="polite">
            <p className="flex min-w-0 items-start gap-2 text-[13px] font-semibold leading-5">
              {riskItems.length > 0
                ? <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                : <CheckCircle2 aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-teal-600" />}
              <span>
                <span>{copy(locale, "riskLabel")}：</span>
                {riskItems.length > 0
                  ? riskItems.join(locale === "en-US" ? "; " : "；")
                  : copy(locale, isRegrading ? "regradeReadyMessage" : "readyMessage")}
              </span>
            </p>
            {riskItems.length > 0 ? (
              <div className="flex shrink-0 gap-4 text-[12px] font-semibold">
                <Link to={`/tasks/${taskId}/questions`} className="hover:underline">{copy(locale, "editQuestions")}</Link>
                <Link to={`/tasks/${taskId}/submissions`} className="hover:underline">{copy(locale, "editSubmissions")}</Link>
              </div>
            ) : null}
          </section>

          <div className="mt-[22px] flex flex-col items-stretch gap-2 pb-8 sm:items-end">
            {historyView ? (
              <>
                <p className="max-w-[600px] text-right text-[12px] leading-5 text-muted-foreground">
                  {locale === "en-US" ? "This is the configuration snapshot used for the completed grading run." : "这里保留本次已执行批改所使用的配置快照，不会再次启动批改。"}
                </p>
                <Link
                  to={`/tasks/${taskId}/review`}
                  className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-[8px] bg-primary px-6 text-[14px] font-semibold text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 sm:w-[180px]"
                >
                  {locale === "en-US" ? "View Grading Review" : "查看复核批改"}
                  <ChevronRight aria-hidden="true" className="h-4 w-4" />
                </Link>
              </>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}

type TaskSummary = {
  problemCount: number;
  studentCount: number;
  criteriaComplete: number;
  answersComplete: number;
  programmingCount: number;
  testsComplete: number;
  flaggedAnswers: number;
  flaggedIdentities: number;
};

function summarizeTask(problems: ProblemInfo[], students: StudentSubmission[]): TaskSummary {
  const programming = problems.filter(isProgrammingProblem);
  return {
    problemCount: problems.length,
    studentCount: students.length,
    criteriaComplete: problems.filter((problem) => Boolean(problem.criterion?.trim())).length,
    answersComplete: problems.filter((problem) => Boolean(problem.reference_answer?.trim())).length,
    programmingCount: programming.length,
    testsComplete: programming.filter((problem) => (problem.test_cases?.length ?? 0) > 0).length,
    flaggedAnswers: students.reduce(
      (count, student) => count + student.stu_ans.filter((answer) => answer.review_status !== "confirmed" && ((answer.flag?.length ?? 0) > 0 || !answer.content?.trim())).length,
      0,
    ),
    flaggedIdentities: students.filter((student) => student.identity_status === "needs_review").length,
  };
}

function isProgrammingProblem(problem: ProblemInfo): boolean {
  const type = (problem.type ?? "").toLowerCase();
  return Boolean(
    problem.solution_code?.trim()
    || (problem.test_cases?.length ?? 0) > 0
    || ["编程", "程序", "代码", "program", "coding", "code"].some((token) => type.includes(token)),
  );
}

function buildRiskItems(summary: TaskSummary, warnings: string[], locale: Locale): string[] {
  const items: string[] = [];
  const criteriaMissing = summary.problemCount - summary.criteriaComplete;
  const answersMissing = summary.problemCount - summary.answersComplete;
  const testsMissing = summary.programmingCount - summary.testsComplete;
  if (criteriaMissing > 0) {
    items.push(locale === "zh-CN"
      ? `${criteriaMissing}${copy(locale, "criteriaMissing")}`
      : criteriaMissing === 1 ? "1 question is missing a rubric" : `${criteriaMissing} questions are missing rubrics`);
  }
  if (answersMissing > 0) {
    items.push(locale === "zh-CN"
      ? `${answersMissing}${copy(locale, "answersMissing")}`
      : answersMissing === 1 ? "1 question is missing a reference answer" : `${answersMissing} questions are missing reference answers`);
  }
  if (testsMissing > 0) {
    items.push(locale === "zh-CN"
      ? `${testsMissing}${copy(locale, "testsMissing")}`
      : testsMissing === 1 ? "1 programming question is missing test cases" : `${testsMissing} programming questions are missing test cases`);
  }
  if (summary.flaggedAnswers > 0) {
    items.push(locale === "zh-CN"
      ? `${summary.flaggedAnswers}${copy(locale, "answersFlagged")}`
      : summary.flaggedAnswers === 1 ? "1 response still has a recognition flag" : `${summary.flaggedAnswers} responses still have recognition flags`);
  }
  if (summary.flaggedIdentities > 0) {
    items.push(locale === "zh-CN"
      ? `${summary.flaggedIdentities}${copy(locale, "identitiesFlagged")}`
      : summary.flaggedIdentities === 1 ? "1 student identity needs confirmation" : `${summary.flaggedIdentities} student identities need confirmation`);
  }
  if (warnings.includes("task_knowledge_empty")) items.push(copy(locale, "knowledgeEmpty"));
  return items;
}

function getDisabledReason({
  locale,
  configured,
  canStart,
  blockingIssues,
  hasEnabledSelection,
}: {
  locale: Locale;
  configured: boolean;
  canStart: boolean;
  blockingIssues: string[];
  hasEnabledSelection: boolean;
}): string | null {
  if (canStart) return null;
  if (!configured || blockingIssues.includes("grading_setup_required")) return copy(locale, "setupRequired");
  if (!hasEnabledSelection || blockingIssues.includes("provider_required") || blockingIssues.includes("provider_not_enabled")) {
    return copy(locale, "configureModels");
  }
  return copy(locale, "unavailable");
}

function compareProblems(left: ProblemInfo, right: ProblemInfo): number {
  return left.number.localeCompare(right.number, undefined, { numeric: true, sensitivity: "base" });
}

function questionOverviewHref(taskId: string, query: string): string {
  const base = `/tasks/${taskId}/questions`;
  return query ? `${base}?q=${encodeURIComponent(query)}` : base;
}

function aggregationLabel(method: GradingSetup["aggregation_method"], locale: Locale): string {
  const labels: Record<GradingSetup["aggregation_method"], [string, string]> = {
    single: ["单专家", "Single model"],
    weighted_average: ["置信度加权", "Confidence weighted"],
    judge_agent: ["裁决专家", "Judge agent"],
  };
  return labels[method][locale === "en-US" ? 1 : 0];
}

function feedbackKey(length: GradingFeedbackLength): "short" | "medium" | "long" {
  return length;
}

function SummaryChip({ children, href, tone }: { children: ReactNode; href: string; tone: "primary" | "success" | "warning" }) {
  return (
    <Link
      to={href}
      className={cn(
        "inline-flex min-h-8 min-w-[90px] items-center justify-center rounded-full px-4 text-[13px] font-semibold outline-none transition hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring",
        tone === "primary" && "bg-blue-100 text-primary dark:bg-blue-950/45",
        tone === "success" && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/45 dark:text-emerald-300",
        tone === "warning" && "bg-amber-100 text-amber-700 dark:bg-amber-950/45 dark:text-amber-300",
      )}
    >
      {children}
    </Link>
  );
}

function StrategyChip({ children, tone }: { children: ReactNode; tone: "success" | "warning" | "neutral" }) {
  return (
    <span className={cn(
      "inline-flex min-h-8 items-center justify-center rounded-full px-4 text-[13px] font-semibold",
      tone === "success" && "bg-teal-100 text-teal-700 dark:bg-teal-950/45 dark:text-teal-300",
      tone === "warning" && "bg-amber-100 text-amber-700 dark:bg-amber-950/45 dark:text-amber-300",
      tone === "neutral" && "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
    )}>
      {children}
    </span>
  );
}

function PageState({
  title,
  busy = false,
  action,
  href,
  onAction,
}: {
  title: string;
  busy?: boolean;
  action?: string;
  href?: string;
  onAction?: () => void;
}) {
  return (
    <section className="mx-auto mt-[34px] flex min-h-[360px] w-full max-w-[1100px] flex-col items-center justify-center rounded-[10px] border bg-card px-6 text-center" aria-busy={busy || undefined}>
      {busy ? <LoaderCircle aria-hidden="true" className="mb-4 h-7 w-7 animate-spin text-primary" /> : null}
      <h2 className="text-base font-semibold text-foreground">{title}</h2>
      {action && href ? (
        <Link to={href} className="mt-5 inline-flex h-9 items-center rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground">{action}</Link>
      ) : action && onAction ? (
        <button type="button" onClick={onAction} className="mt-5 h-9 rounded-[7px] border bg-card px-4 text-sm font-semibold hover:bg-muted">{action}</button>
      ) : null}
    </section>
  );
}
