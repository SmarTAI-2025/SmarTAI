import { AlertTriangle, ArrowRight, CheckCircle2, ChevronRight, LoaderCircle, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { useConfirmTaskFinalization, useTask, useTaskFinalization, useTaskResult, useTeacherComments } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { MatrixQueueWorkspace } from "@/components/tasks/MatrixQueueWorkspace";
import { MatrixStatusCell, type MatrixStatusTone } from "@/components/tasks/MatrixStatusCell";
import { getMatrixIdentityLayout, MATRIX_ACTION_COLUMN_WIDTH, MATRIX_QUESTION_COLUMN_WIDTH } from "@/components/tasks/matrixLayout";
import { buildResultsModel, effectiveCorrectionScore, formatConfidence, formatPercent, type QuestionSummary, type ResultsModel, type StudentSummary } from "@/components/tasks/resultsModel";
import { collectResultReviewItems, type ReviewItem } from "@/components/tasks/resultsReviewModel";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { isExpertDisagreement, reviewCellKey, selectReviewOverview } from "@/lib/reviewOverview";
import { reviewOverviewText as copy } from "@/lib/reviewOverviewCopy";
import { getTaskDestination, hasTaskReachedStep } from "@/lib/taskFlow";
import type { Correction } from "@/types";

/** R01: compact Figma-14 review overview backed only by persisted grading results. */
export function ReviewOverviewPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
  const resultQuery = useTaskResult(taskId);
  const commentsQuery = useTeacherComments(taskId);
  const finalizationQuery = useTaskFinalization(taskId);
  const confirmFinalization = useConfirmTaskFinalization();
  const urlQuery = searchParams.get("q") ?? "";
  const [draftQuery, setDraftQuery] = useState(urlQuery);
  const [query, setQuery] = useState(urlQuery.trim());
  const composingRef = useRef(false);
  const searchParamsRef = useRef(searchParams);
  const task = taskQuery.data;
  const model = useMemo(() => buildResultsModel(task, resultQuery.data), [resultQuery.data, task]);
  const reviewItems = useMemo(() => collectResultReviewItems(model, model.students), [model]);
  const annotatedKeys = useMemo(() => {
    const keys = new Set(Object.entries(commentsQuery.data?.comments ?? {}).filter(([, comment]) => comment.trim()).map(([key]) => key));
    for (const student of model.students) {
      for (const correction of student.corrections) {
        if (correction.teacher_comment?.trim()) keys.add(reviewCellKey(student.id, correction.q_id));
      }
    }
    return keys;
  }, [commentsQuery.data?.comments, model.students]);
  const confirmedKeys = useMemo(() => new Set(
    model.students.flatMap((student) => student.corrections
      .filter((correction) => correction.review_status === "confirmed")
      .map((correction) => reviewCellKey(student.id, correction.q_id))),
  ), [model.students]);
  const selection = useMemo(
    () => selectReviewOverview(model, reviewItems, annotatedKeys, query),
    [annotatedKeys, model, query, reviewItems],
  );

  useEffect(() => {
    searchParamsRef.current = searchParams;
    if (composingRef.current) return;
    setDraftQuery(urlQuery);
    setQuery(urlQuery.trim());
  }, [searchParams, urlQuery]);

  if (taskId && task && !hasTaskReachedStep(task, 6)) {
    if (task.status === "grading") return <Navigate replace to={`/tasks/${taskId}/grading/progress`} />;
    return <Navigate replace to={getTaskDestination(task)} />;
  }

  const isLoading = taskQuery.isLoading || resultQuery.isLoading || finalizationQuery.isLoading;
  const isError = taskQuery.isError || resultQuery.isError || finalizationQuery.isError;
  const pendingReviewItems = reviewItems.filter((item) => !confirmedKeys.has(reviewCellKey(item.student.id, item.question.id)));
  const historyView = Boolean(task && task.status !== "graded");
  const overviewReturnTo = taskId
    ? `/tasks/${encodeURIComponent(taskId)}/review${searchParams.toString() ? `?${searchParams.toString()}` : ""}`
    : "";
  const firstTarget = pendingReviewItems[0]
    ?? (model.students[0] && model.questions[0]
      ? { student: model.students[0], question: model.questions[0] } as Pick<ReviewItem, "student" | "question">
      : null);
  const targetHref = taskId && firstTarget
    ? reviewDetailHref(taskId, firstTarget.student.id, firstTarget.question.id, overviewReturnTo)
    : null;
  const correctionCount = model.students.reduce((total, student) => total + student.corrections.length, 0);
  const disagreementCount = model.students.reduce(
    (total, student) => total + student.corrections.filter(isExpertDisagreement).length,
    0,
  );
  const remainingReviewCount = finalizationQuery.data?.remaining_review_count ?? pendingReviewItems.length;
  const readyForConfirmation = finalizationQuery.data?.ready_for_confirmation === true;
  const editedPendingCount = pendingReviewItems.filter((item) => item.correction.review_status === "edited").length;
  const lockedResultsReason = remainingReviewCount > 0
    ? copy(locale, "lockedResultsRemaining").replace("{count}", String(remainingReviewCount))
    : copy(locale, "lockedResultsReady");

  function confirmReviewComplete() {
    if (!taskId || !readyForConfirmation || confirmFinalization.isPending) return;
    confirmFinalization.mutate({
      taskId,
      expectedWorkflowRevision: finalizationQuery.data?.workflow_revision ?? task?.workflow_revision ?? 0,
    }, {
      onSuccess: () => navigate(`/tasks/${taskId}/results`),
    });
  }

  function activateLockedResults() {
    toast.info(lockedResultsReason);
    if (remainingReviewCount > 0 && targetHref) {
      navigate(targetHref);
      return;
    }
    const confirmButton = document.getElementById("confirm-review-complete");
    confirmButton?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.requestAnimationFrame(() => confirmButton?.focus());
  }

  function submitFilter(event: FormEvent) {
    event.preventDefault();
    commitFilter(draftQuery);
  }

  function commitFilter(value: string) {
    const normalized = value.trim();
    setQuery(normalized);
    const next = new URLSearchParams(searchParamsRef.current);
    if (normalized) next.set("q", normalized);
    else next.delete("q");
    searchParamsRef.current = next;
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {copy(locale, "title")}
      </h1>
      <NewTaskStepper
        currentStep={6}
        lockedStep={7}
        lockedStepReason={lockedResultsReason}
        onLockedStepActivate={activateLockedResults}
      />

      {!taskId ? (
        <PageState title={copy(locale, "missingTask")} href="/history" action={copy(locale, "viewHistory")} />
      ) : isLoading ? (
        <PageState title={copy(locale, "loading")} busy />
      ) : isError ? (
        <PageState
          title={copy(locale, "loadError")}
          description={copy(locale, "loadErrorDescription")}
          action={copy(locale, "retry")}
          onAction={() => { void Promise.all([taskQuery.refetch(), resultQuery.refetch(), commentsQuery.refetch(), finalizationQuery.refetch()]); }}
        />
      ) : !model.students.length || !model.questions.length ? (
        <PageState title={copy(locale, "empty")} href="/history" action={copy(locale, "viewHistory")} />
      ) : (
        <>
          <div className="mt-5 grid grid-cols-2 gap-4 xl:grid-cols-4 xl:gap-5">
            <MetricCard value={formatMetricPercent(model.classAveragePercent)} label={copy(locale, "average")} tone="primary" />
            <MetricCard value={String(model.lowConfidenceCount)} label={copy(locale, "lowConfidence")} tone="warning" />
            <MetricCard value={String(disagreementCount)} label={copy(locale, "disagreement")} tone="primary" />
            <MetricCard value={`${confirmedKeys.size}/${correctionCount}`} label={copy(locale, "annotated")} tone="accent" />
          </div>

          {!historyView && confirmFinalization.isError ? (
            <p role="alert" className="mt-3 text-sm font-medium text-destructive">
              {locale === "en-US" ? "The task changed. Refresh the review state and try again." : "任务状态已变化，请刷新复核状态后重试。"}
            </p>
          ) : null}

          <form onSubmit={submitFilter} role="search" className="mt-6">
            <label className="relative block">
              <span className="sr-only">{copy(locale, "searchLabel")}</span>
              <Search aria-hidden="true" className="pointer-events-none absolute left-5 top-1/2 h-5 w-5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={draftQuery}
                inputMode="search"
                onCompositionStart={() => { composingRef.current = true; }}
                onCompositionEnd={(event) => {
                  composingRef.current = false;
                  const value = event.currentTarget.value;
                  setDraftQuery(value);
                  window.setTimeout(() => commitFilter(value), 0);
                }}
                onChange={(event) => {
                  const value = event.target.value;
                  setDraftQuery(value);
                  if (!composingRef.current) commitFilter(value);
                }}
                placeholder={copy(locale, "searchPlaceholder")}
                className="h-12 w-full rounded-[10px] border bg-card pl-14 pr-28 text-[14px] text-foreground outline-none transition placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
              />
              {query ? (
                <button
                  type="button"
                  onClick={() => { setDraftQuery(""); commitFilter(""); }}
                  className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md px-3 py-1.5 text-xs font-semibold text-primary hover:bg-primary/5"
                >
                  {copy(locale, "clear")}
                </button>
              ) : null}
            </label>
          </form>

          {query ? (
            <p className="mt-2 text-[12px] text-muted-foreground" aria-live="polite">
              {selection.explanation === "no-match"
                ? copy(locale, "noMatch")
                : copy(locale, "filtered")
                    .replace("{students}", String(selection.students.length))
                    .replace("{questions}", String(selection.questions.length))
                    .replace("{cells}", String(selection.matchedCellKeys.size))}
            </p>
          ) : null}

          <MatrixQueueWorkspace
            className={query ? "mt-5" : "mt-7"}
            matrix={(
              <ReviewHeatmap
                locale={locale}
                taskId={taskId}
                model={model}
                students={selection.students}
                questions={selection.questions}
                matchedCellKeys={selection.matchedCellKeys}
                reviewItems={reviewItems}
                annotatedKeys={annotatedKeys}
                confirmedKeys={confirmedKeys}
                returnTo={overviewReturnTo}
              />
            )}
            queue={(
              <ReviewQueue
                locale={locale}
                taskId={taskId}
                items={pendingReviewItems.filter((item) => selection.matchedCellKeys.has(reviewCellKey(item.student.id, item.question.id)))}
                returnTo={overviewReturnTo}
              />
            )}
          />

          <footer className="mt-4 flex min-h-[58px] flex-col gap-3 rounded-[10px] border bg-card px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between xl:px-5">
            <p className="text-xs leading-5 text-muted-foreground">
              {historyView
                ? copy(locale, "historyHint")
                : remainingReviewCount > 0
                  ? editedPendingCount > 0
                    ? copy(locale, "editedRemainingHint")
                        .replace("{count}", String(remainingReviewCount))
                        .replace("{edited}", String(editedPendingCount))
                    : copy(locale, "remainingHint").replace("{count}", String(remainingReviewCount))
                  : copy(locale, "readyHint")}
            </p>
            <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
              {targetHref && (historyView || readyForConfirmation) ? (
                <Link
                  to={targetHref}
                  className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring sm:w-auto"
                >
                  {copy(locale, "viewDetails")}
                </Link>
              ) : null}
              {historyView ? (
                <Link
                  to={`/tasks/${taskId}/results`}
                  className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 sm:w-auto"
                >
                  {copy(locale, "viewFinalResults")}
                  <ArrowRight aria-hidden="true" className="h-4 w-4" />
                </Link>
              ) : remainingReviewCount > 0 && targetHref ? (
                <Link
                  to={targetHref}
                  className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 sm:w-auto"
                >
                  {copy(locale, "continueReview").replace("{count}", String(remainingReviewCount))}
                  <ArrowRight aria-hidden="true" className="h-4 w-4" />
                </Link>
              ) : (
                <button
                  id="confirm-review-complete"
                  type="button"
                  disabled={!readyForConfirmation || confirmFinalization.isPending}
                  title={!readyForConfirmation ? copy(locale, "confirmDisabled") : undefined}
                  onClick={confirmReviewComplete}
                  className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
                >
                  {confirmFinalization.isPending ? copy(locale, "confirming") : copy(locale, "confirmReview")}
                  {confirmFinalization.isPending
                    ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
                    : <CheckCircle2 aria-hidden="true" className="h-4 w-4" />}
                </button>
              )}
            </div>
          </footer>
        </>
      )}
    </div>
  );
}

function MetricCard({ value, label, tone }: { value: string; label: string; tone: "primary" | "warning" | "accent" }) {
  return (
    <section className="flex min-h-[90px] min-w-0 flex-col justify-center rounded-[10px] border bg-card px-5">
      <strong className={cn(
        "text-[28px] font-bold leading-8 tracking-[-0.02em]",
        tone === "primary" && "text-primary",
        tone === "warning" && "text-amber-500",
        tone === "accent" && "text-teal-500",
      )}>{value}</strong>
      <span className="mt-2 text-[13px] font-medium text-muted-foreground">{label}</span>
    </section>
  );
}

function ReviewHeatmap({
  locale,
  taskId,
  model,
  students,
  questions,
  matchedCellKeys,
  reviewItems,
  annotatedKeys,
  confirmedKeys,
  returnTo,
}: {
  locale: Locale;
  taskId: string;
  model: ResultsModel;
  students: StudentSummary[];
  questions: QuestionSummary[];
  matchedCellKeys: Set<string>;
  reviewItems: ReviewItem[];
  annotatedKeys: Set<string>;
  confirmedKeys: Set<string>;
  returnTo: string;
}) {
  const reviewByKey = new Map(reviewItems.map((item) => [reviewCellKey(item.student.id, item.question.id), item]));
  const questionById = new Map(model.questions.map((question) => [question.id, question]));
  const studentIdLabel = copy(locale, "studentId");
  const studentNameLabel = copy(locale, "studentName");
  const identityLayout = getMatrixIdentityLayout({
    questionCount: questions.length,
    studentIds: students.map((student) => student.id),
    studentNames: students.map((student) => student.name !== student.id ? student.name : "—"),
    studentIdLabel,
    studentNameLabel,
  });
  const tableMinWidth = Math.max(
    740,
    identityLayout.studentIdWidth
      + identityLayout.studentNameWidth
      + MATRIX_ACTION_COLUMN_WIDTH
      + questions.length * MATRIX_QUESTION_COLUMN_WIDTH,
  );

  return (
    <section className="h-[330px] min-w-0 overflow-hidden rounded-[10px] border bg-card" aria-labelledby="review-heatmap-title">
      <h2 id="review-heatmap-title" className="sr-only">{copy(locale, "heatmap")}</h2>
      {!students.length || !questions.length ? (
        <div className="flex h-full flex-col items-center justify-center px-8 text-center">
          <Search aria-hidden="true" className="h-7 w-7 text-muted-foreground" />
          <p className="mt-3 max-w-md text-sm text-muted-foreground">{copy(locale, "noMatch")}</p>
        </div>
      ) : (
        <div className="h-full overflow-auto overscroll-contain">
          <table
            className="w-full border-collapse text-left text-[13px]"
            style={{ minWidth: `${tableMinWidth}px` }}
          >
            <thead className="sticky top-0 z-20 bg-slate-100/95 text-[12px] font-semibold text-muted-foreground backdrop-blur-sm dark:bg-slate-800/95">
              <tr className="h-[42px] border-b">
                <th
                  className="sticky left-0 z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
                  style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
                >
                  {studentIdLabel}
                </th>
                <th
                  className="sticky z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
                  style={{ left: identityLayout.studentIdWidth, width: identityLayout.studentNameWidth, minWidth: identityLayout.studentNameWidth, maxWidth: identityLayout.studentNameWidth }}
                >
                  {studentNameLabel}
                </th>
                {questions.map((question) => (
                  <th key={question.id} className="w-[60px] min-w-[60px] max-w-[60px] px-1 text-center">
                    {question.label}
                  </th>
                ))}
                <th className="w-[72px] px-3 text-right">{copy(locale, "action")}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {students.map((student) => {
                const correctionByQuestion = new Map(student.corrections.map((correction) => [correction.q_id, correction]));
                const entryQuestion = questions.find((question) => {
                  const key = reviewCellKey(student.id, question.id);
                  return reviewByKey.has(key) && !confirmedKeys.has(key);
                }) ?? questions[0];
                const entryHref = entryQuestion
                  ? reviewDetailHref(taskId, student.id, entryQuestion.id, returnTo)
                  : null;
                return (
                  <tr key={student.id} className="h-[52px] bg-card transition-colors hover:bg-muted/25">
                    <th
                      className="sticky left-0 z-10 whitespace-nowrap bg-card px-3 text-[12px] font-semibold text-foreground"
                      style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
                      title={student.id}
                    >
                      {student.id}
                    </th>
                    <td
                      className="sticky z-10 whitespace-nowrap bg-card px-3 text-[12px] text-muted-foreground"
                      style={{ left: identityLayout.studentIdWidth, width: identityLayout.studentNameWidth, minWidth: identityLayout.studentNameWidth, maxWidth: identityLayout.studentNameWidth }}
                      title={student.name}
                    >
                      {student.name !== student.id ? student.name : "—"}
                    </td>
                    {questions.map((question) => {
                      const correction = correctionByQuestion.get(question.id);
                      const key = reviewCellKey(student.id, question.id);
                      if (!correction || !matchedCellKeys.has(key)) return <td key={question.id} className="h-[52px] w-[60px] min-w-[60px] max-w-[60px] px-1" />;
                      return (
                        <td key={question.id} className="h-[52px] w-[60px] min-w-[60px] max-w-[60px] px-1">
                          <ReviewCell
                            locale={locale}
                            href={reviewDetailHref(taskId, student.id, question.id, returnTo)}
                            correction={correction}
                            item={reviewByKey.get(key)}
                            annotated={annotatedKeys.has(key)}
                            confirmed={confirmedKeys.has(key)}
                            question={questionById.get(question.id)}
                          />
                        </td>
                      );
                    })}
                    <td className="w-[72px] px-3 text-right">
                      {entryHref ? (
                        <Link
                          to={entryHref}
                          className="text-xs font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          {copy(locale, "view")}
                        </Link>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ReviewCell({ locale, href, correction, item, annotated, confirmed, question }: { locale: Locale; href: string; correction: Correction; item?: ReviewItem; annotated: boolean; confirmed: boolean; question?: QuestionSummary }) {
  const state = confirmed ? "confirmed" : correction.review_status === "edited" ? "edited" : annotated ? "commented" : item?.category === "low-confidence" ? "low" : item ? "review" : "ok";
  const label = copy(locale, state);
  const detail = `${question?.label ?? correction.q_id} · ${formatPercent(correction.max_score > 0 ? (effectiveCorrectionScore(correction) / correction.max_score) * 100 : null)} · ${formatConfidence(correction.confidence)}`;
  const tone: MatrixStatusTone = state === "confirmed"
    ? "reviewed"
    : state === "edited"
      ? "warning"
      : state === "commented"
      ? "note"
      : state === "low"
        ? "warning"
        : state === "review"
          ? "error"
          : "ok";
  return <MatrixStatusCell to={href} label={`${label} · ${detail}`} tone={tone} />;
}

function ReviewQueue({ locale, taskId, items, returnTo }: { locale: Locale; taskId: string; items: ReviewItem[]; returnTo: string }) {
  return (
    <section className="h-[330px] overflow-hidden rounded-[10px] border bg-card px-4 py-5" aria-labelledby="review-queue-title">
      <h2 id="review-queue-title" className="text-[16px] font-bold leading-6 text-foreground">{copy(locale, "queue")}</h2>
      {!items.length ? (
        <div className="flex h-[248px] flex-col items-center justify-center text-center">
          <CheckCircle2 aria-hidden="true" className="h-7 w-7 text-teal-500" />
          <p className="mt-3 max-w-[220px] text-[12px] leading-5 text-muted-foreground">{copy(locale, "noQueue")}</p>
        </div>
      ) : (
        <ol className="mt-3 h-[250px] space-y-1 overflow-y-auto overscroll-contain pr-1">
          {items.map((item) => (
            <li key={reviewCellKey(item.student.id, item.question.id)}>
              <Link
                to={reviewDetailHref(taskId, item.student.id, item.question.id, returnTo)}
                className="flex min-h-[54px] items-center gap-2 rounded-[8px] px-2 py-1.5 text-[12px] outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-semibold text-foreground" title={`${item.student.id} ${item.student.name}`}>
                    {item.student.name} · {item.question.label}
                  </span>
                  <span className="mt-0.5 block truncate text-muted-foreground">{queueReason(locale, item)}</span>
                </span>
                <ChevronRight aria-hidden="true" className="h-4 w-4 shrink-0 text-muted-foreground" />
              </Link>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function PageState({ title, description, busy = false, action, href, onAction }: { title: string; description?: string; busy?: boolean; action?: string; href?: string; onAction?: () => void }) {
  const content = action && href ? (
    <Link to={href} className="mt-5 inline-flex h-10 items-center rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground">{action}</Link>
  ) : action && onAction ? (
    <button type="button" onClick={onAction} className="mt-5 inline-flex h-10 items-center rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground">{action}</button>
  ) : null;
  return (
    <section className="mx-auto mt-6 flex min-h-[360px] max-w-[940px] flex-col items-center justify-center rounded-[10px] border bg-card px-6 text-center" aria-busy={busy || undefined}>
      {busy ? <LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" /> : <AlertTriangle aria-hidden="true" className="h-7 w-7 text-muted-foreground" />}
      <h2 className="mt-4 text-base font-semibold text-foreground">{title}</h2>
      {description ? <p className="mt-2 max-w-lg text-sm leading-6 text-muted-foreground">{description}</p> : null}
      {content}
    </section>
  );
}

function queueReason(locale: Locale, item: ReviewItem): string {
  if (item.correction.review_status === "edited") return copy(locale, "savedPendingReason");
  if (item.category === "low-confidence") return copy(locale, "lowReason");
  if (item.category === "expert-disagreement") return copy(locale, "disagreementReason");
  if (item.category === "score-anomaly") return copy(locale, "anomalyReason");
  return copy(locale, "reviewReason");
}

function reviewDetailHref(taskId: string, studentId: string, questionId: string, returnTo?: string): string {
  const base = `/tasks/${encodeURIComponent(taskId)}/review/${encodeURIComponent(studentId)}/${encodeURIComponent(questionId)}`;
  return returnTo ? `${base}?returnTo=${encodeURIComponent(returnTo)}` : base;
}

function formatMetricPercent(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}
