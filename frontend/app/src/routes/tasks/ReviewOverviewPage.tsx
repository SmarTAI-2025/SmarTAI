import { ReviewQueue as ReviewQueuePanel } from "@/components/tasks/ReviewQueue";
import { isCorrectionReviewConfirmed, correctionReviewBlocked, correctionReviewNextBlocked } from "@/components/tasks/resultsReviewModel";
import { ReviewConfirmButton, ReviewStageActions, RetryFailedGrading } from "@/components/tasks/ReviewConfirmation";
import { SortableTableHead, useColumnSort, sortColumnRows, directionFor, type ColumnSort } from "@/components/ui/SortableTableHead";
import { AlertTriangle, ArrowRight, CheckCircle2, ChevronRight, LoaderCircle, Search } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useConfirmResultReviews } from "@/hooks/useConfirmResultReviews";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useTask, useTaskFinalization, useTaskResult, useTeacherComments } from "@/api/hooks/tasks";
import { TaskQueryBar } from "@/components/tasks/AskQueryBar";
import { useTaskFilterIntent } from "@/hooks/useTaskFilterIntent";
import { EMPTY_FILTER_INTENT, supportsFilterIntent } from "@/lib/taskFilterIntent";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { MatrixViewLink } from "@/components/tasks/MatrixViewLink";
import { MatrixQueueWorkspace } from "@/components/tasks/MatrixQueueWorkspace";
import { MatrixStatusCell, type MatrixStatusTone } from "@/components/tasks/MatrixStatusCell";
import { getMatrixIdentityLayout, MATRIX_ACTION_COLUMN_WIDTH, MATRIX_QUESTION_COLUMN_WIDTH } from "@/components/tasks/matrixLayout";
import { buildResultsModel, hasPartialExperts, effectiveCorrectionScore, formatConfidence, formatPercent, type QuestionSummary, type ResultsModel, type StudentSummary } from "@/components/tasks/resultsModel";
import { collectResultReviewItems, type ReviewItem } from "@/components/tasks/resultsReviewModel";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { isExpertDisagreement, reviewCellKey, resolveReviewFilter, selectReviewOverview, selectReviewOverviewFromIntent } from "@/lib/reviewOverview";
import { reviewOverviewText as copy } from "@/lib/reviewOverviewCopy";
import { getTaskDestination, hasTaskReachedStep } from "@/lib/taskFlow";
import type { Correction, FilterIntentResult } from "@/types";

/** R01: compact Figma-14 review overview backed only by persisted grading results. */
export function ReviewOverviewPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId, { refetchOnMount: "always" });
  const resultQuery = useTaskResult(taskId);
  const commentsQuery = useTeacherComments(taskId);
  const finalizationQuery = useTaskFinalization(taskId);
  const confirmReviews = useConfirmResultReviews();
  const confirmingRef = useRef(false);
  const [blockedDialog, setBlockedDialog] = useState(false);
  const [batchError, setBatchError] = useState("");
  const urlQuery = searchParams.get("q") ?? "";
  const query = urlQuery.trim();
  const task = taskQuery.data;
  // Progress polling can finish before the detail cache is refreshed. Wait for
  // that read before redirecting back, just as the two recognition overviews do.
  const readingCompletion = taskQuery.isFetching && task && !hasTaskReachedStep(task, 6);
  const model = useMemo(() => buildResultsModel(task, resultQuery.data), [resultQuery.data, task]);
  const reviewItems = useMemo(() => collectResultReviewItems(model, model.students, locale).filter(item => !isCorrectionReviewConfirmed(item.correction)), [model, locale]);
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
      .filter(isCorrectionReviewConfirmed)
      .map((correction) => reviewCellKey(student.id, correction.q_id))),
  ), [model.students]);
  const smartFilter = useTaskFilterIntent({ taskId, surface: "review_overview", resolveLocal: (value) => resolveReviewFilter(model, reviewItems, annotatedKeys, value) });
  const naturalSelection = useMemo(() => {
    const intent = smartFilter.intent ?? EMPTY_FILTER_INTENT;
    const requested = searchParams.get("sort") as FilterIntentResult["sort"];
    const withSort = requested ? { ...intent, sort: requested } : intent;
    return selectReviewOverviewFromIntent(model, reviewItems, annotatedKeys, supportsFilterIntent(withSort, "review_overview") ? withSort : intent);
  }, [annotatedKeys, model, reviewItems, searchParams, smartFilter.intent]);

  const rawSort = searchParams.get("sort") ?? smartFilter.intent?.sort;
  const headerSort = useColumnSort(["id", "name", ...model.questions.map((question) => `question:${question.id}`)],
    rawSort && /^(?:id|name)_(?:asc|desc)$/.test(rawSort) ? { key: rawSort.split("_")[0], direction: rawSort.endsWith("desc") ? "desc" : "asc" } : null,
    smartFilter.cancel);
  const selection = useMemo(() => ({ ...naturalSelection,
    students: sortColumnRows(naturalSelection.students, headerSort.current, (student, key) => {
      if (key === "id") return student.id;
      if (key === "name") return student.name;
      const correction = student.corrections.find((item) => item.q_id === key.slice("question:".length));
      if (!correction) return null;
      if (effectiveCorrectionScore(correction) === null) return 3;
      if (confirmedKeys.has(reviewCellKey(student.id, correction.q_id))) return 0;
      return reviewItems.some((item) => item.student.id === student.id && item.question.id === correction.q_id) ? 2 : 1;
    }),
  }), [naturalSelection, headerSort.current?.key, headerSort.current?.direction, confirmedKeys, reviewItems]);

  // Failed runs must first enter manual recovery; a legacy query flag cannot bypass it.
  if (taskId && task && !taskQuery.isFetching && !taskQuery.isError && !hasTaskReachedStep(task, 6)) {
    if (task.status === "grading") return <Navigate replace to={`/tasks/${taskId}/grading/progress`} />;
    return <Navigate replace to={getTaskDestination(task)} />;
  }

  const isLoading = taskQuery.isLoading || readingCompletion || resultQuery.isLoading || finalizationQuery.isLoading;
  const isError = taskQuery.isError || resultQuery.isError || finalizationQuery.isError;
  const pendingReviewItems = reviewItems.filter((item) => !confirmedKeys.has(reviewCellKey(item.student.id, item.question.id)));
  const pausedGrading = pendingReviewItems.some(item => item.correction.initial_review_reasons?.includes("grading_paused") || item.correction.review_reasons?.includes("grading_paused"));
  const blockingReviewItems = pendingReviewItems.filter((item) => correctionReviewNextBlocked(item.correction));
  const historyView = Boolean(task && task.status !== "graded");
  const overviewReturnTo = taskId
    ? `/tasks/${encodeURIComponent(taskId)}/review${searchParams.toString() ? `?${searchParams.toString()}` : ""}`
    : "";
  const visibleReviewItems = pendingReviewItems.filter((item) => selection.matchedCellKeys.has(reviewCellKey(item.student.id, item.question.id)));
  const firstTarget = visibleReviewItems.find((item) => effectiveCorrectionScore(item.correction) === null) ?? visibleReviewItems[0]
    ?? (selection.students[0] && selection.questions[0]
      ? { student: selection.students[0], question: selection.questions[0] } as Pick<ReviewItem, "student" | "question">
      : null);
  const targetHref = taskId && firstTarget
    ? reviewDetailHref(taskId, firstTarget.student.id, firstTarget.question.id, overviewReturnTo)
    : null;
  const disagreementCount = model.students.reduce(
    (total, student) => total + student.corrections.filter(correction => !isCorrectionReviewConfirmed(correction) && isExpertDisagreement(correction)).length,
    0,
  );
  const remainingReviewCount = Math.max(finalizationQuery.data?.remaining_review_count ?? 0, blockingReviewItems.length);
  const readyForConfirmation = finalizationQuery.data?.ready_for_confirmation === true && blockingReviewItems.length === 0;
  const confirmableReviews = model.students.flatMap((student) => student.corrections.flatMap((correction) => {
    const score = effectiveCorrectionScore(correction);
    const teacherConfirmed = Number.isFinite(correction.teacher_score) && correction.review_status === "confirmed";
    return !teacherConfirmed && visibleReviewItems.some(item => item.student.id === student.id && item.question.id === correction.q_id && item.severity === "advisory") && score !== null && score >= 0 && score <= correction.max_score
      ? [{ studentId: student.id, qId: correction.q_id, score, comment: correction.teacher_comment ?? "" }]
      : [];
  }));
  // Find unresolved scores across the whole task, even when Ask has filtered them out.
  const unresolvedReview = blockingReviewItems[0];
  const unresolvedHref = taskId && unresolvedReview
    ? reviewDetailHref(taskId, unresolvedReview.student.id, unresolvedReview.question.id, overviewReturnTo)
    : null;
  const lockedResultsReason = remainingReviewCount > 0
    ? (locale === "zh-CN" ? `还有 ${remainingReviewCount} 个关键问题需要处理并确认，暂不能查看结果分析。` : `${remainingReviewCount} required items need correction and confirmation before Results & Analysis.`)
    : copy(locale, "readyHint");
  function viewResults() {
    if (taskId && readyForConfirmation) navigate(`/tasks/${taskId}/results`);
  }

  async function confirmAllReviews() {
    if (!taskId || confirmingRef.current) return;
    if (!confirmableReviews.length) {
      if (remainingReviewCount > 0) setBlockedDialog(true);
      return;
    }
    confirmingRef.current = true;
    setBatchError("");
    try {
      await confirmReviews.mutateAsync({
        taskId, revision: task?.workflow_revision ?? 0, entries: confirmableReviews,
      });
      toast.success(locale === "en-US" ? `${confirmableReviews.length} reviews confirmed.` : `已确认 ${confirmableReviews.length} 个题次的复核。`);
    } catch (error) {
      setBatchError(normalizeAPIError(error).message);
    } finally {
      confirmingRef.current = false;
    }
  }

  function activateLockedResults() {
    if (remainingReviewCount > 0) {
      setBlockedDialog(true);
      return;
    }
    toast.info(lockedResultsReason);
    const confirmButton = document.getElementById("confirm-review-complete");
    confirmButton?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.requestAnimationFrame(() => confirmButton?.focus());
  }

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {copy(locale, "title")}
      </h1>
      <NewTaskStepper
        currentStep={6}
        blockedFromStep={remainingReviewCount > 0 ? 7 : undefined}
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
          {!historyView && resultQuery.data?.grading_run_status === "partial_failed" ? (
            <section className="mt-5 rounded-[10px] border border-warning/40 bg-warning/5 p-5" aria-label={pausedGrading ? (locale === "zh-CN" ? "继续未完成批改" : "Resume incomplete grading") : (locale === "zh-CN" ? "批改失败重试" : "Retry failed grading")}>
              <h2 className="mb-2 text-base font-semibold">{locale === "zh-CN" ? "部分作答未完成批改" : "Some answers could not be graded"}</h2>
              <RetryFailedGrading taskId={taskId} revision={task?.workflow_revision ?? 0} locale={locale} paused={pausedGrading} />
            </section>
          ) : null}
          <div className="mt-5 grid grid-cols-2 gap-4 xl:grid-cols-4 xl:gap-5">
            <MetricCard value={formatMetricPercent(model.classAveragePercent)} label={copy(locale, "average")} tone="primary" />
            <MetricCard value={String(pendingReviewItems.filter(item => item.category === "low-confidence").length)} label={copy(locale, "lowConfidence")} tone="warning" />
            <MetricCard value={String(disagreementCount)} label={copy(locale, "disagreement")} tone="primary" />
            <MetricCard value={`${confirmedKeys.size}/${model.students.reduce((total, student) => total + student.corrections.length, 0)}`} label={copy(locale, "annotated")} tone="accent" />
          </div>

          <TaskQueryBar className="mt-6" filter={smartFilter} taskId={taskId} locale={locale}
            label={locale === "zh-CN" ? "Ask SmarTAI：复核批改" : "Ask SmarTAI: grading review"}
            placeholder={copy(locale, "searchPlaceholder")} />



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
                columnSort={headerSort.current}
                onSort={headerSort.toggle}
              />
            )}
            queue={(
              <ReviewQueue confirmation={<ReviewConfirmButton locale={locale} all confirmed={!confirmableReviews.length && remainingReviewCount === 0} busy={confirmReviews.isPending} onClick={() => void confirmAllReviews()} />}
                locale={locale}
                taskId={taskId}
                items={pendingReviewItems.filter((item) => selection.matchedCellKeys.has(reviewCellKey(item.student.id, item.question.id)))}
                returnTo={overviewReturnTo}
              />
            )}
          />

          <ReviewStageActions
            note={remainingReviewCount > 0
              ? (locale === "zh-CN" ? `还有 ${remainingReviewCount} 个关键问题需要处理并确认。` : `${remainingReviewCount} required items need correction and confirmation.`)
              : copy(locale, "readyHint")}

            back={{ href: targetHref ?? `/tasks/${taskId}/review`, label: locale === "zh-CN" ? "查看批改详情" : "View Grading Details" }}
            next={{ id: "confirm-review-complete", label: locale === "zh-CN" ? "查看结果分析" : "View Results & Analysis", disabled: confirmReviews.isPending, onClick: () => readyForConfirmation ? viewResults() : setBlockedDialog(true) }}
          >
            {batchError ? <p role="alert" className="mt-3 text-sm text-destructive">{locale === "en-US"
              ? `Confirmed ${confirmReviews.progress.completed}/${confirmReviews.progress.total}; the rest were not confirmed. ${batchError}`
              : `已确认 ${confirmReviews.progress.completed}/${confirmReviews.progress.total}，其余未确认。${batchError}`}</p> : null}
          </ReviewStageActions>
        </>
      )}
      {blockedDialog ? <UnsavedChangesDialog
        title={locale === "en-US" ? "Some results need correction and confirmation" : "还有结果需要处理并确认"}
        description={lockedResultsReason}
        stayLabel={locale === "en-US" ? "Stay here" : "留在此页"}
        leaveLabel={locale === "en-US" ? "Go to the missing score" : "直接前往填写分数"}
        onStay={() => setBlockedDialog(false)}
        onLeave={() => { setBlockedDialog(false); if (unresolvedHref) navigate(unresolvedHref); else void finalizationQuery.refetch(); }}
      >
        {taskId && blockingReviewItems.some(item => effectiveCorrectionScore(item.correction) === null) ? <div className="mt-4"><RetryFailedGrading taskId={taskId} revision={task?.workflow_revision ?? 0} locale={locale} paused={pausedGrading} /></div> : null}
        <ul className="mt-3 max-h-48 space-y-2 overflow-auto text-sm">{blockingReviewItems.map(item => <li key={reviewCellKey(item.student.id, item.question.id)}><Link className="text-primary underline" onClick={() => setBlockedDialog(false)} to={reviewDetailHref(taskId!, item.student.id, item.question.id, overviewReturnTo)}>{item.student.name} · {item.question.label} · {locale === "zh-CN" ? "请处理并确认" : "Correct and confirm"}</Link></li>)}</ul>
      </UnsavedChangesDialog> : null}
    </div>
  );
}

function MetricCard({ value, label, tone }: { value: string; label: string; tone: "primary" | "warning" | "accent" }) {
  return (
    <section className="flex min-h-[90px] min-w-0 flex-col justify-center rounded-[10px] border bg-card px-5">
      <strong className={cn(
        "text-[28px] font-bold leading-8 tracking-[-0.02em]",
        tone === "primary" && "text-primary",
        tone === "warning" && (value === "0" ? "text-teal-500" : "text-red-500"),
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
  columnSort,
  onSort,
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
  columnSort: ColumnSort | null;
  onSort: (key: string) => void;
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
                <SortableTableHead
                  className="sticky left-0 z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
                  style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
                 direction={directionFor(columnSort, "id")} onSort={() => onSort("id")} locale={locale}>{studentIdLabel}</SortableTableHead>
                <SortableTableHead
                  className="sticky z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
                  style={{ left: identityLayout.studentIdWidth, width: identityLayout.studentNameWidth, minWidth: identityLayout.studentNameWidth, maxWidth: identityLayout.studentNameWidth }}
                 direction={directionFor(columnSort, "name")} onSort={() => onSort("name")} locale={locale}>{studentNameLabel}</SortableTableHead>
                {questions.map((question) => (
                  <SortableTableHead direction={directionFor(columnSort, `question:${question.id}`)} onSort={() => onSort(`question:${question.id}`)} label={question.label} locale={locale} title={locale === "zh-CN" ? "按该题复核状态排序" : "Sort by this question’s review status"} key={question.id} className="w-[60px] min-w-[60px] max-w-[60px] px-1 text-center">
                    {question.label}
                  </SortableTableHead>
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
                        <MatrixViewLink to={entryHref} locale={locale} />
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
  const state = correctionReviewNextBlocked(correction) ? "hard" : confirmed ? "confirmed" : item ? "review" : "ready";
  const label = locale === "zh-CN" ? (state === "hard" ? "需处理并确认" : state === "confirmed" ? "已确认" : state === "review" ? "待复核" : "已批改") : (state === "hard" ? "Correction and confirmation required" : state === "confirmed" ? "Confirmed" : state === "review" ? "Needs review" : "Graded");
  const displayScore = effectiveCorrectionScore(correction);
  const scoreDetail = displayScore !== null && correction.max_score > 0
    ? `${formatPercent((displayScore / correction.max_score) * 100)} · `
    : "";
  const detail = `${question?.label ?? correction.q_id} · ${scoreDetail}${formatConfidence(correction.confidence)}${item ? ` · ${queueReason(locale, item)}` : ""}`;
  const tone: MatrixStatusTone = state === "hard" ? "error" : state === "confirmed" ? "reviewed" : state === "review" ? "warning" : "ok";
  return <MatrixStatusCell to={href} label={`${label} · ${detail}`} tone={tone} />;
}

function ReviewQueue({ locale, taskId, items, returnTo, confirmation }: { confirmation: React.ReactNode; locale: Locale; taskId: string; items: ReviewItem[]; returnTo: string }) {
  return <ReviewQueuePanel locale={locale} confirmation={confirmation} items={items.map(item => ({
    key: reviewCellKey(item.student.id, item.question.id), title: `${item.student.name} · ${item.question.label}`,
    reason: queueReason(locale, item), severity: item.severity,
    href: reviewDetailHref(taskId, item.student.id, item.question.id, returnTo),
  }))} />;
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
  if (item.correction.initial_review_reasons?.includes("grading_paused") || item.correction.review_reasons?.includes("grading_paused")) {
    return effectiveCorrectionScore(item.correction) === null
      ? (locale === "zh-CN" ? "已暂停尚未批改 · 请人工补齐" : "Paused, ungraded · complete manually")
      : (locale === "zh-CN" ? "部分专家完成 · 已保存有效评分和评语" : "Some experts completed · valid scores and feedback saved");
  }
  if (effectiveCorrectionScore(item.correction) === null) return locale === "zh-CN" ? "缺少有效分数 · 需处理" : "Missing valid score · action required";
  if (correctionReviewBlocked(item.correction)) return locale === "zh-CN" ? "请核对补齐后的分数并确认" : "Check the corrected score and confirm";
  if (item.severity === "blocking") return locale === "zh-CN" ? "请核对并确认" : "Check and confirm the result";
  if (item.correction.review_status === "edited") return copy(locale, "savedPendingReason");
  if (hasPartialExperts(item.correction)) return locale === "zh-CN" ? "部分专家完成 · 建议核对" : "Some experts completed · review suggested";
  if (item.category === "low-confidence") return locale === "zh-CN" ? "置信度较低 · 建议核对" : "Low confidence · review suggested";
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
