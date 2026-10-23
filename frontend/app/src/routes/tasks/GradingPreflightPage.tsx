import { userFacingError } from "@/lib/taskActionGuards";
import { ManualReviewButton } from "@/components/tasks/ManualReviewButton";
import { questionReviewBlockers, submissionReviewBlockers } from "@/lib/reviewConfirmation";
import { AlertTriangle, ArrowLeft, CheckCircle2, ChevronRight, LoaderCircle, Timer } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { getAPIErrorCode } from "@/api/client";
import { useGradingSetup, useStartGrading, useTask } from "@/api/hooks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { GradingRetryNotice } from "@/components/tasks/GradingRetryNotice";
import { SubmissionSourceOutcomePanel } from "@/components/tasks/SubmissionSourceOutcomePanel";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { useI18n } from "@/i18n/I18nProvider";
import { useGradingIntent } from "@/hooks/useGradingIntent";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { gradingPreflightText as copy } from "@/lib/gradingPreflightCopy";
import { modelDisplayName } from "@/lib/modelPresentation";
import {
  canTaskBeRegraded,
  isTaskAwaitingGrading,
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
  const intent = useGradingIntent();
  const [countdown, setCountdown] = useState(AUTO_START_SECONDS);
  const [autoStartEnabled, setAutoStartEnabled] = useState(true);
  const [showBlocker, setShowBlocker] = useState(false);
  const startTriggeredRef = useRef(false);
  const startHandlerRef = useRef<() => void>(() => undefined);

  useLayoutEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [taskId]);

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
  const pendingIdentityStudent = students.find((student) => student.identity_status === "needs_review");
  const pendingAnswerStudent = students.find((student) => student.stu_ans.some((answer) => answer.review_status !== "confirmed"));
  const pendingAnswer = pendingAnswerStudent?.stu_ans.find((answer) => answer.review_status !== "confirmed");
  const submissionReviewHref = pendingAnswerStudent && pendingAnswer
    ? `/tasks/${encodeURIComponent(taskId ?? "")}/students/${encodeURIComponent(pendingAnswerStudent.stu_id)}?${new URLSearchParams({ question: pendingAnswer.q_id })}`
    : `/tasks/${taskId}/submissions`;
  const selectedExperts = useMemo(() => {
    if (!setup || !setupResponse) return [];
    const selected = new Set(setup.selected_provider_ids);
    return setupResponse.available_experts.filter((expert) => selected.has(expert.provider_id));
  }, [setup, setupResponse]);
  const riskItems = useMemo(
    () => buildRiskItems(summary, setupResponse?.readiness.warnings ?? [], locale),
    [locale, setupResponse?.readiness.warnings, summary],
  );
  const failedGrading = (task?.status === "error" || task?.status === "grading_paused") && Boolean(task.grading_job_id)
    && (!task.last_failed_job_id || task.last_failed_job_id === task.grading_job_id);
  const isRegrading = canTaskBeRegraded(task?.status) || failedGrading;
  const historyView = Boolean(task && !isTaskAwaitingGrading(task.status) && !isRegrading);

  const blockingIssues = setupResponse?.readiness.blocking_issues ?? [];
  const localQuestionBlockers = questionReviewBlockers(problems, taskId ?? "", locale);
  const localSubmissionBlockers = submissionReviewBlockers(task, locale);
  const effectiveBlockingIssues = [...blockingIssues,
    ...(localQuestionBlockers.length ? ["question_review_blocked"] : []),
    ...(localSubmissionBlockers.length ? ["submission_recognition_blocked"] : [])];
  const hasSourceWarnings = (setupResponse?.readiness.warnings ?? []).some(
    (warning) => warning.startsWith("submission_"),
  );
  const hasReviewWarnings = summary.flaggedQuestions > 0 || summary.flaggedAnswers > 0
    || summary.flaggedIdentities > 0 || hasSourceWarnings;
  const hasEnabledSelection = selectedExperts.length > 0
    && selectedExperts.length === setup?.selected_provider_ids.length
    && selectedExperts.every((expert) => expert.enabled);
  const canStart = Boolean(
    taskId
    && (isTaskAwaitingGrading(task?.status) || isRegrading)
    && setupResponse?.configured
    && setup
    && effectiveBlockingIssues.length === 0
    && hasEnabledSelection
    && summary.problemCount > 0
    && summary.studentCount > 0,
  );
  // Automatic and manual start share the same blockers; review advice is informational.
  const countdownActive = canStart && autoStartEnabled && !startGrading.isPending;

  async function handleStart() {
    if (!taskId || !task || startTriggeredRef.current) return;
    if (!canStart) {
      setShowBlocker(true);
      return;
    }
    startTriggeredRef.current = true;
    setAutoStartEnabled(false);
    try {
      const response = await intent.execute(task.workflow_revision, (request) => startGrading.mutateAsync({ taskId, ...request }));
      if (!response) return;
      if (response.status === "already_done" || (response.status === "already_finished" && ["completed", "partial_failed"].includes(response.run_status ?? ""))) {
        navigate(`/tasks/${taskId}/review`, { replace: true });
        return;
      }
      navigate(`/tasks/${taskId}/grading/progress`, { replace: true });
    } catch {
      await Promise.allSettled([taskQuery.refetch(), setupQuery.refetch()]);
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
  const startError = startGrading.error
    ? gradingBlockerMessage(getAPIErrorCode(startGrading.error), locale)
      ?? userFacingError(startGrading.error, locale)
    : null;
  const disabledReason = historyView ? null : getDisabledReason({
    locale,
    configured: setupResponse?.configured ?? false,
    canStart,
    blockingIssues: effectiveBlockingIssues,
    hasEnabledSelection,
  });
  const recoveryCode = !setupResponse?.configured || effectiveBlockingIssues.includes("grading_setup_required")
    ? "grading_setup_required"
    : !hasEnabledSelection || effectiveBlockingIssues.some((issue) => ["provider_required", "provider_not_enabled"].includes(issue))
      ? "provider_required"
      : effectiveBlockingIssues[0] ?? (summary.problemCount === 0 ? "questions_required" : summary.studentCount === 0 ? "submissions_required" : undefined);
  const recovery = taskId ? gradingRecovery(taskId, recoveryCode, locale) : null;

  if (taskQuery.isSuccess && taskId && task) {
    if (task.status === "grading") {
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
        <RecoverableActionState info={classifyRecoverableError(taskQuery.error ?? setupQuery.error, { locale, taskId })} locale={locale} workflowRecovery={{ retry: { onClick: () => { void taskQuery.refetch(); void setupQuery.refetch(); } }, configurationHref: taskId ? getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/preflight`) : "/history" }} />
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
                    disabled={startGrading.isPending}
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
        <ManualReviewButton taskId={taskId} task={taskQuery.data} stage="grading" />
              {startError ? <RecoverableActionState usageContext={{ providerIds: setup?.selected_provider_ids ?? [], providers: setupResponse?.available_experts ?? [] }} info={classifyRecoverableError(startGrading.error, { locale, taskId, returnTo: `/tasks/${taskId}/grading/preflight` })} locale={locale} compact workflowRecovery={{ retry: { onClick: () => void handleStart(), busy: startGrading.isPending }, configurationHref: getTaskGradingSetupHref(taskId, `/tasks/${taskId}/grading/preflight`) }} /> : null}
              {isRegrading ? <div className="border-t px-6 py-3 sm:px-8"><GradingRetryNotice locale={locale} /></div> : null}
            </section>
          ) : null}

          <section className={cn(
            "mt-[30px] flex min-h-[62px] flex-col gap-3 rounded-[10px] border px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-8",
            effectiveBlockingIssues.length > 0
              ? "border-red-400 bg-red-50 text-red-950 dark:bg-red-950/20 dark:text-red-100"
              : "border-primary/20 bg-primary/5 text-foreground",
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
              <div className="flex shrink-0 flex-wrap gap-4 text-[12px] font-semibold">
                {summary.flaggedQuestions > 0 ? <Link to={summary.flaggedQuestions
                  ? `/tasks/${taskId}/questions/${encodeURIComponent(problems.find((problem) => problem.review_status !== "confirmed")!.q_id)}/content`
                  : `/tasks/${taskId}/questions`} className="hover:underline">{copy(locale, "editQuestions")}</Link> : null}
                {pendingIdentityStudent ? <Link to={`/tasks/${encodeURIComponent(taskId ?? "")}/students/${encodeURIComponent(pendingIdentityStudent.stu_id)}?identity=edit`} className="hover:underline">{locale === "zh-CN" ? "核对姓名学号" : "Review student identity"}</Link> : null}
                {summary.flaggedAnswers > 0 || hasSourceWarnings ? <Link to={submissionReviewHref} className="hover:underline">{copy(locale, "editSubmissions")}</Link> : null}
              </div>
            ) : null}
            {countdownActive && hasReviewWarnings ? <p className="text-xs">{locale === "zh-CN" ? "如需核对，请在倒计时结束前返回。" : "To review, go back before the countdown ends."}</p> : null}
          </section>

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

          {task.submission_sources?.length ? (
            <div className="mt-[30px]">
              <SubmissionSourceOutcomePanel
                summary={task.submission_source_summary}
                sources={task.submission_sources}
                locale={locale}
                taskId={taskId}
              />
            </div>
          ) : null}

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
                    {setup?.expert_weights?.[expert.provider_id] !== undefined ? ` · ${locale === "zh-CN" ? "权重" : "weight"} ${setup.expert_weights[expert.provider_id]}` : ""}
                  </span>
                ))}
                {setup ? (
                  <span className="inline-flex min-h-8 items-center rounded-full bg-teal-100 px-4 text-[13px] font-semibold text-teal-700 dark:bg-teal-950/45 dark:text-teal-300">
                    {copy(locale, "aggregation")}: {aggregationLabel(setup.aggregation_method, locale)}{setup.selected_provider_ids.length > 1 ? ` · ${Object.keys(setup.expert_weights ?? {}).length ? (locale === "zh-CN" ? "自定义权重" : "Custom weights") : (locale === "zh-CN" ? "置信度权重" : "Confidence weights")}` : ""}
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
                  {locale === "zh-CN" ? "任一专家置信度 < 0.60 时建议处理" : "Any expert confidence < 0.60: suggested review"}
                </StrategyChip>
                <StrategyChip tone="neutral">
                  {copy(locale, "feedback")}: {copy(locale, feedbackKey(setup.feedback_length))}
                </StrategyChip>
              </div>
            ) : (
              <p className="mt-5 text-sm text-danger">{copy(locale, "setupRequired")}</p>
            )}
          </section>



          <div className="mt-[22px] flex flex-col items-stretch gap-2 pb-8 sm:items-end">
            {historyView ? (
              <>
                <p className="max-w-[600px] text-right text-[12px] leading-5 text-muted-foreground">
                  {locale === "en-US" ? "Settings used for this grading run." : "本次批改使用的设置。"}
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
      {showBlocker && recovery ? (
        <UnsavedChangesDialog
          title={locale === "zh-CN" ? "还需要处理一项内容" : "One more thing before grading"}
          description={disabledReason ?? copy(locale, "unavailable")}
          stayLabel={locale === "zh-CN" ? "关闭" : "Close"}
          leaveLabel={recovery.label}
          onStay={() => setShowBlocker(false)}
          onLeave={() => navigate(recovery.href)}
        />
      ) : null}
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
  flaggedQuestions: number;
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
    flaggedQuestions: problems.filter((problem) => problem.review_status !== "confirmed").length,
    flaggedAnswers: students.reduce(
      (count, student) => count + student.stu_ans.filter((answer) => answer.review_status !== "confirmed").length,
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
  if (summary.flaggedQuestions > 0) {
    items.push(locale === "zh-CN" ? `${summary.flaggedQuestions}道题目待确认`
      : summary.flaggedQuestions === 1 ? "1 question awaits confirmation" : `${summary.flaggedQuestions} questions await confirmation`);
  }
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
      : summary.flaggedAnswers === 1 ? "1 response still needs review" : `${summary.flaggedAnswers} responses await confirmation`);
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
  const blocker = blockingIssues
    .map((issue) => gradingBlockerMessage(issue, locale))
    .find((message): message is string => Boolean(message));
  if (blocker) return blocker;
  return copy(locale, "unavailable");
}

function gradingBlockerMessage(code: string | null, locale: Locale): string | null {
  const messages: Record<string, [string, string]> = {
    question_review_blocked: ["题目存在关键缺项或冲突。请修正并确认后再开始批改。", "Questions have required missing information or conflicts. Resolve and confirm them before grading."],
    submission_recognition_blocked: ["学生身份或作答识别存在关键问题。请修正并确认后再开始批改。", "Student identities or recognized responses have blocking issues. Resolve and confirm them before grading."],
    submission_sources_failed: ["仍有文件识别失败。请在上方查看具体原因并重新处理后再开始批改。", "Some files failed recognition. Review the exact reasons above and resolve them before grading."],
    submission_sources_pending: ["还有作答文件未处理完，请稍后再检查。", "Some submission files are still being processed. Please check again later."],
    submission_source_evidence_missing: ["作答来源状态需要重新确认。请刷新；若结构化题目和作答仍在，可直接重试批改。", "The submission source state needs confirmation. Refresh; if the structured questions and answers remain, retry grading directly."],
    submissions_required: ["当前没有可批改的学生作答。", "There are no student submissions to grade."],
    answers_required: ["至少一名学生缺少当前结构化作答。请先检查作答校对结果。", "At least one student has no current structured answers. Review the submission results first."],
    questions_required: ["当前任务还没有题目。", "This task has no questions yet."],
    workflow_busy: ["任务正在执行另一项操作，请等待完成后刷新。", "Another task operation is running. Wait for it to finish, then refresh."],
    workflow_revision_conflict: ["任务内容已变化。请刷新后按最新内容重新确认。", "The task changed. Refresh and confirm the latest content."],
    ocr_provider_grading_not_supported: ["该 OCR 服务不支持批改，请更换批改模型。", "This OCR service does not support grading. Choose a grading model."],
  };
  const message = code ? messages[code] : undefined;
  return message ? message[locale === "en-US" ? 1 : 0] : null;
}

function gradingRecovery(taskId: string, code: string | undefined, locale: Locale) {
  const root = `/tasks/${taskId}`;
  if (code === "question_review_blocked") return { href: `${root}/questions`, label: locale === "zh-CN" ? "审核题目" : "Review questions" };
  if (code === "submission_recognition_blocked") return { href: `${root}/submissions`, label: locale === "zh-CN" ? "校对作答" : "Review submissions" };
  if (code === "questions_required") return { href: `${root}/upload/problems`, label: locale === "zh-CN" ? "添加题目" : "Add questions" };
  if (code === "submissions_required" || code === "submission_sources_failed") return { href: `${root}/submissions/upload`, label: locale === "zh-CN" ? "处理作答文件" : "Fix submission files" };
  if (code === "answers_required" || code === "submission_source_evidence_missing") return { href: `${root}/submissions`, label: locale === "zh-CN" ? "查看作答" : "Review submissions" };
  if (code === "submission_sources_pending") return { href: `${root}/submissions/progress`, label: locale === "zh-CN" ? "查看识别进度" : "View recognition progress" };
  if (code === "workflow_busy" || code === "grading_setup_locked") return { href: root, label: locale === "zh-CN" ? "查看当前进度" : "View current progress" };
  return { href: getTaskGradingSetupHref(taskId, `${root}/grading/preflight`), label: locale === "zh-CN" ? "修改批改设置" : "Fix grading settings" };
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
    weighted_average: ["加权平均", "Weighted average"],
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
