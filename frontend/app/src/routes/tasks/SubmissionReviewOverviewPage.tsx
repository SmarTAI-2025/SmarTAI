import { ReviewQueue as ReviewQueuePanel } from "@/components/tasks/ReviewQueue";
import { ReviewConfirmButton, ReviewBlockDialog, ReviewStageActions, reviewActionClass } from "@/components/tasks/ReviewConfirmation";
import { submissionReviewBlockers, submissionRecognitionIncomplete, answerReviewBlocked, studentIdentityReviewBlocked, type ReviewBlocker } from "@/lib/reviewConfirmation";
import { SortableTableHead, useColumnSort, sortColumnRows, directionFor, type ColumnSort } from "@/components/ui/SortableTableHead";
import { AlertCircle, CheckCircle2, ChevronRight, RotateCcw, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useTask, useUpdateStudentAnswer, useManuallyCompleteSubmissionRecognition, useAddTaskStudent, useDeleteTaskStudent } from "@/api/hooks/tasks";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { getAPIErrorCode } from "@/api/client";
import { confirmSubmissionBatch } from "@/lib/submissionReviewBatch";
import { answerNeedsConfirmation, answerReviewAttention, answerReviewLabels } from "@/lib/submissionReview";
import { isWorkflowRevisionConflictCode } from "@/lib/taskActionGuards";
import { TaskQueryBar } from "@/components/tasks/AskQueryBar";
import { useTaskFilterIntent } from "@/hooks/useTaskFilterIntent";
import { EMPTY_FILTER_INTENT, supportsFilterIntent } from "@/lib/taskFilterIntent";
import { resolveSubmissionQuery, selectSubmissionQuestions } from "@/lib/taskPreparationFilter";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { MatrixViewLink } from "@/components/tasks/MatrixViewLink";
import { MatrixQueueWorkspace } from "@/components/tasks/MatrixQueueWorkspace";
import { MatrixStatusCell, type MatrixStatusTone } from "@/components/tasks/MatrixStatusCell";
import { getMatrixIdentityLayout, MATRIX_ACTION_COLUMN_WIDTH, MATRIX_QUESTION_COLUMN_WIDTH } from "@/components/tasks/matrixLayout";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import {
  answerMap,
  buildSubmissionQuestions,
  getAnswerState,
  formatSubmissionFlag,
  getSubmissionReviewStats,
  studentNeedsAttention,
  type SubmissionAnswerState,
  type SubmissionQuestion,
  type SubmissionReviewFilter,
  type SubmissionReviewSelection,
  type SubmissionReviewSort,
} from "@/lib/submissionReview";
import { isTaskAwaitingGrading, getTaskDestination, hasTaskReachedStep } from "@/lib/taskFlow";
import type { FilterIntentResult, StudentAnswerInfo, StudentSubmission, SubmissionSourceOutcome } from "@/types";

const EXPLANATION_KEYS: Record<SubmissionReviewSelection["explanation"], MessageKey> = {
  all: "submissionReviewFilterAllHint",
  student: "submissionReviewFilterStudentHint",
  question: "submissionReviewFilterQuestionHint",
  review: "submissionReviewFilterReviewHint",
  missing: "submissionReviewFilterMissingHint",
  identity: "submissionReviewFilterIdentityHint",
  no_match: "submissionReviewFilterNoMatchHint",
};

interface SubmissionQueueItem {
  key: string;
  href: string;
  studentId: string;
  studentName: string;
  questionLabel: string;
  reasonKey: MessageKey;
  severity: "blocking" | "advisory";
  reason?: string;
}

export function SubmissionReviewOverviewPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale, t } = useI18n();
  // A completed /state poll may arrive before the cached task detail changes.
  const taskQuery = useTask(taskId, { refetchOnMount: "always" });
  const readingCompletion = taskQuery.isFetching && !hasTaskReachedStep(taskQuery.data, 4);
  const answerMutation = useUpdateStudentAnswer();
  const manualMutation = useManuallyCompleteSubmissionRecognition();
  const addStudent = useAddTaskStudent();
  const deleteStudent = useDeleteTaskStudent();
  const [deletingStudent, setDeletingStudent] = useState<StudentSubmission | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const rowBusy = addStudent.isPending || deleteStudent.isPending;
  const canEditRows = isTaskAwaitingGrading(taskQuery.data?.status) && !taskQuery.data?.manual_student_count_required;

  const [studentCount, setStudentCount] = useState("");
  const [countError, setCountError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<ReviewBlocker[]>([]);
  const batchLock = useRef(false);
  const [batchProgress, setBatchProgress] = useState<{ completed: number; total: number } | null>(null);
  const [batchResult, setBatchResult] = useState<{ message: string; href?: string } | null>(null);
  const query = searchParams.get("q") ?? "";
  const latestSearchParamsRef = useRef(new URLSearchParams(searchParams));
  const filter: SubmissionReviewFilter = "all";
  const sort = normalizeSort(searchParams.get("sort"));

  const students = useMemo(
    () => Object.values(taskQuery.data?.student_data ?? {}),
    [taskQuery.data?.student_data],
  );
  const questions = useMemo(
    () => buildSubmissionQuestions(Object.values(taskQuery.data?.problem_data ?? {}), students),
    [students, taskQuery.data?.problem_data],
  );
  const stats = useMemo(() => getSubmissionReviewStats(students, questions), [questions, students]);
  const pendingIdentities = students.filter((student) => student.identity_status === "needs_review" || studentIdentityReviewBlocked(student, taskQuery.data?.submission_sources));
  const pendingAnswers = students.flatMap((student) => (student.stu_ans ?? [])
    .filter(answerNeedsConfirmation)
    .map((answer) => ({ student, answer })));
  const advisoryCandidates = pendingAnswers.filter(({ student, answer }) => answerReviewAttention(answer) === "advisory"
    && !studentIdentityReviewBlocked(student, taskQuery.data?.submission_sources));
  const smartFilter = useTaskFilterIntent({
    taskId, surface: "submission_review",
    resolveLocal: (value) => resolveSubmissionQuery(students, questions, value),
  });
  const naturalSelection = useMemo(() => {
    const current = smartFilter.intent ?? EMPTY_FILTER_INTENT;
    const requested = searchParams.get("sort");
    const aliases: Record<string, FilterIntentResult["sort"]> = { student_id: "id_asc", student_name: "name_asc", attention: "review_desc" };
    const withSort = { ...current, sort: (requested ? aliases[requested] ?? requested : current.sort ?? "id_asc") as FilterIntentResult["sort"] };
    return selectSubmissionQuestions(students, questions, supportsFilterIntent(withSort, "submission_review") ? withSort : current, filter);
  }, [filter, questions, searchParams, smartFilter.intent, students]);

  const rawSort = searchParams.get("sort") ?? smartFilter.intent?.sort ?? "id_asc";
  const headerSort = useColumnSort(["id", "name", ...questions.map((question) => `question:${question.id}`)],
    /^(?:id|name)_(?:asc|desc)$/.test(rawSort) ? { key: rawSort.split("_")[0], direction: rawSort.endsWith("desc") ? "desc" : "asc" }
      : rawSort === "student_id" || rawSort === "student_name" ? { key: rawSort === "student_id" ? "id" : "name", direction: "asc" } : null,
    smartFilter.cancel);
  const selection = useMemo(() => ({ ...naturalSelection,
    students: sortColumnRows(naturalSelection.students, headerSort.current, (student, key) => {
      if (key === "name") return student.stu_name || student.stu_id;
      if (key === "id") return student.stu_id;
      const state = getAnswerState(answerMap(student).get(key.slice("question:".length)));
      return { reviewed: 0, recognized: 1, flagged: 2, empty: 3, missing: 4 }[state];
    }),
  }), [naturalSelection, headerSort.current?.key, headerSort.current?.direction]);

  useEffect(() => { latestSearchParamsRef.current = new URLSearchParams(searchParams); }, [searchParams]);

  if (taskQuery.isSuccess && !taskQuery.isFetching && taskId) {
    if (!hasTaskReachedStep(taskQuery.data, 4) || submissionRecognitionIncomplete(taskQuery.data)) {
      return <Navigate replace to={getTaskDestination(taskQuery.data)} />;
    }
  }

  function setParam(key: string, value: string, defaultValue: string) {
    smartFilter.cancel();
    const next = new URLSearchParams(latestSearchParamsRef.current);
    if (!value || value === defaultValue) next.delete(key);
    else next.set(key, value);
    latestSearchParamsRef.current = next;
    setSearchParams(next, { replace: true });
  }

  const attentionStudent = selection.students.find((student) => studentNeedsAttention(student, selection.questions));
  const detailStudent = attentionStudent ?? selection.students[0];
  const detailQuestion = detailStudent
    ? firstReviewQuestion(detailStudent, selection.questions)
    : null;
  const returnSearch = searchParams.toString();
  const queueItems = taskId
    ? buildSubmissionQueueItems(selection.students, selection.questions, taskId, returnSearch, locale, taskQuery.data?.submission_sources)
    : [];
  const pendingReviews = advisoryCandidates.filter(({ student, answer }) => queueItems.some(item => item.key === `${student.stu_id}:${answer.q_id}` && item.severity === "advisory"));

  async function changeStudentRow(student?: StudentSubmission) {
    if (!taskId || !taskQuery.data || rowBusy) return;
    setRowError(null);
    try {
      const input = { taskId, expectedWorkflowRevision: taskQuery.data.workflow_revision };
      if (student) await deleteStudent.mutateAsync({ ...input, studentId: student.stu_id });
      else {
        await addStudent.mutateAsync(input);
        smartFilter.cancel();
        latestSearchParamsRef.current = new URLSearchParams();
        setSearchParams({}, { replace: true });
      }
      setDeletingStudent(null);
      await taskQuery.refetch();
    } catch {
      setRowError(locale === "zh-CN" ? "修改学生行失败，请刷新核对任务后重试。" : "Could not update student rows. Refresh the task and retry.");
    }
  }
  const addStudentButton = canEditRows ? <button type="button" disabled={rowBusy || !questions.length} onClick={() => void changeStudentRow()} className={reviewActionClass}>
    <Plus aria-hidden="true" className="h-4 w-4" />{locale === "zh-CN" ? "添加学生" : "Add student"}
  </button> : null;

  async function createStudentRows() {
    if (!taskId || !taskQuery.data?.parse_job_id || manualMutation.isPending) return;
    const count = Number(studentCount);
    if (!Number.isInteger(count) || count < 1 || count > 500) return;
    setCountError(null);
    try {
      await manualMutation.mutateAsync({ taskId, jobId: taskQuery.data.parse_job_id,
        expectedWorkflowRevision: taskQuery.data.workflow_revision, studentCount: count });
      await taskQuery.refetch();
    } catch {
      setCountError(locale === "zh-CN" ? "建立学生行失败，请刷新核对任务后重试。" : "Could not create student rows. Refresh the task and retry.");
    }
  }

  async function confirmAll() {
    if (!taskId || !taskQuery.data || batchLock.current) return;
    batchLock.current = true;
    setBatchResult(null);
    const total = pendingReviews.length;
    setBatchProgress({ completed: 0, total });
    try {
      const progress = (completed: number) => setBatchProgress({ completed, total });
      const revision = taskQuery.data.workflow_revision;
      const result = await confirmSubmissionBatch(pendingReviews, revision, ({ student, answer }, expectedWorkflowRevision) => answerMutation.mutateAsync({ taskId, studentId: student.stu_id, qId: answer.q_id, reviewStatus: "confirmed", expectedWorkflowRevision }), progress);
      const unit = locale === "zh-CN" ? "项" : "items";
      if (result.error) {
        const code = getAPIErrorCode(result.error);
        const reason = isWorkflowRevisionConflictCode(code)
          ? (locale === "zh-CN" ? "任务内容已更新，已停止确认；请核对最新内容后继续。" : "Task changed. Review the latest content before continuing.")
          : code === "workflow_busy"
            ? (locale === "zh-CN" ? "任务正在处理，请完成后再试。" : "The task is busy. Retry after it finishes.")
            : (locale === "zh-CN" ? "确认失败，请打开未完成的记录检查后重试。" : "Confirmation failed. Open the unfinished record and retry.");
        const failed = result.failedItem;
        const target = failed?.student;
        setBatchResult({
          message: locale === "zh-CN" ? `已确认 ${result.completed} ${unit}，剩余 ${result.remaining} 项未确认。${reason}` : `Confirmed ${result.completed} ${unit}; ${result.remaining} remain. ${reason}`,
          href: target ? (failed?.answer ? studentReviewPath(taskId, target.stu_id, failed.answer.q_id, returnSearch) : identityReviewPath(taskId, target.stu_id, returnSearch)) : undefined,
        });
        await taskQuery.refetch();
      } else {
        setBatchResult({ message: locale === "zh-CN" ? `已确认全部 ${result.completed} ${unit}。` : `Confirmed all ${result.completed} ${unit}.` });
      }
    } finally {
      batchLock.current = false;
      setBatchProgress(null);
    }
  }

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {t("submissionReviewTitle")}
      </h1>
      {deletingStudent ? <UnsavedChangesDialog title={locale === "zh-CN" ? "删除此学生行？" : "Delete this student row?"}
        description={locale === "zh-CN" ? `将从当前复核矩阵移除 ${deletingStudent.stu_name || ""}（${deletingStudent.stu_id}）。` : `Remove ${deletingStudent.stu_name || ""} (${deletingStudent.stu_id}) from the current review matrix.`}
        stayLabel={locale === "zh-CN" ? "取消" : "Cancel"} leaveLabel={locale === "zh-CN" ? "删除" : "Delete"}
        saving={rowBusy} onStay={() => { if (!rowBusy) setDeletingStudent(null); }} onLeave={() => void changeStudentRow(deletingStudent)}>
        {rowError ? <p role="alert" className="text-sm text-danger">{rowError}</p> : null}
      </UnsavedChangesDialog> : null}
      {rowError && !deletingStudent ? <p role="alert" className="mt-3 text-sm text-danger">{rowError}</p> : null}
      <ReviewBlockDialog locale={locale} issues={blocked} onClose={() => setBlocked([])} />
      <NewTaskStepper currentStep={4} blockedFromStep={submissionReviewBlockers(taskQuery.data, locale).length ? 5 : undefined} lockedStepReason={locale === "zh-CN" ? "请先处理并确认关键问题" : "Resolve and confirm required items first"} onLockedStepActivate={() => setBlocked(submissionReviewBlockers(taskQuery.data, locale))} />


      <section className="mt-[22px] min-w-0" aria-labelledby="submission-review-matrix">
        <h2 id="submission-review-matrix" className="sr-only">{t("submissionReviewMatrixLabel")}</h2>

        <dl className="grid min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 lg:gap-5">
          <ReviewMetric
            label={t("submissionReviewMetricIdentity")}
            value={formatPercent(stats.identityMatched, stats.students)}
            detail={`${stats.identityMatched}/${stats.students}`}
            tone={stats.identityAnomalies > 0 ? "danger" : "accent"}
          />
          <ReviewMetric
            label={t("submissionReviewMetricCoverage")}
            value={formatPercent(stats.answeredCells, stats.expectedCells)}
            detail={`${stats.answeredCells}/${stats.expectedCells}`}
          />
          <ReviewMetric
            label={t("submissionReviewMetricReview")}
            value={String(stats.reviewCells)}
            detail={locale === "zh-CN" ? t("submissionReviewMetricCells") : stats.reviewCells === 1 ? "response" : "responses"}
            tone={queueItems.some(item => item.severity === "blocking") ? "danger" : "primary"}
          />
          <ReviewMetric
            label={t("submissionReviewMetricIdentityIssues")}
            value={String(stats.identityAnomalies)}
            detail={locale === "zh-CN" ? t("submissionReviewMetricStudents") : stats.identityAnomalies === 1 ? "student" : "students"}
            tone={stats.identityAnomalies > 0 ? "danger" : "neutral"}
          />
        </dl>

        {isTaskAwaitingGrading(taskQuery.data?.status) && students.length > 0 ? (
          <div className="mt-4 rounded-[10px] border bg-card p-4">
            <div className="flex flex-wrap items-center gap-2">
              <p role="status" className="text-sm">{locale === "zh-CN" ? `待确认：${pendingIdentities.length} 位学生身份、${pendingAnswers.length} 个作答题次` : `Awaiting confirmation: ${pendingIdentities.length} identities, ${pendingAnswers.length} responses`}</p>

            </div>
            <p className="mt-2 text-xs text-muted-foreground">{locale === "zh-CN" ? "一键确认仅处理当前“建议处理”队列，不修改作答内容；“需要处理”中的问题请逐项处理。" : "Confirm all acknowledges the visible Suggested review items without changing answers. Resolve Action required items individually."}</p>
            {batchProgress ? <p role="status" className="mt-2 text-sm">{locale === "zh-CN" ? "正在确认" : "Confirming"} {batchProgress.completed}/{batchProgress.total}</p> : null}
            {batchResult ? <p role="status" className="mt-2 text-sm">{batchResult.message}{batchResult.href ? <Link className="ml-2 text-primary underline" to={batchResult.href}>{locale === "zh-CN" ? "打开未完成记录" : "Open unfinished record"}</Link> : null}</p> : null}
          </div>
        ) : null}

        {!isTaskAwaitingGrading(taskQuery.data?.status) && batchResult ? <p role="status" className="mt-3 text-sm">{batchResult.message}</p> : null}
        <TaskQueryBar className="mt-6" filter={smartFilter} taskId={taskId} locale={locale}
          label={locale === "zh-CN" ? "Ask SmarTAI：学生作答" : "Ask SmarTAI: student answers"}
          placeholder={locale === "zh-CN" ? "找出缺答的学生，或按覆盖率排序" : "Find missing answers, or sort by coverage"} />
        <div className="mt-2 flex min-h-5 items-start justify-between gap-3 px-1">
          <p className="text-[11px] leading-5 text-muted-foreground">
            {selection.explanation !== "all" ? t(EXPLANATION_KEYS[selection.explanation]) : null}
            {selection.confidenceAlias ? ` ${t("submissionReviewConfidenceAliasHint")}` : ""}
          </p>
          {query || filter !== "all" || searchParams.has("sort") || searchParams.has("column_sort") ? (
            <button
              type="button"
              onClick={() => {
                smartFilter.cancel();
                latestSearchParamsRef.current = new URLSearchParams();
                setSearchParams({}, { replace: true });
              }}
              className="inline-flex shrink-0 items-center gap-1 text-[11px] font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
            >
              <RotateCcw aria-hidden="true" className="h-3 w-3" />
              {t("submissionReviewClearFilters")}
            </button>
          ) : null}
        </div>

        <div className="mt-4 min-w-0">
          {taskQuery.isLoading || readingCompletion ? (
            <section className="overflow-hidden rounded-[10px] border bg-card">
              <MatrixState title={t("submissionReviewLoading")} busy />
            </section>
          ) : taskQuery.isError ? (
            <section className="overflow-hidden rounded-[10px] border bg-card">
              <MatrixState
                title={t("submissionReviewLoadError")}
                action={t("submissionReviewRetry")}
                onAction={() => void taskQuery.refetch()}
              />
            </section>
          ) : !taskId ? (
            <section className="overflow-hidden rounded-[10px] border bg-card">
              <MatrixState title={t("submissionReviewTaskMissing")} />
            </section>
          ) : students.length === 0 && taskQuery.data?.manual_student_count_required ? (
            <form className="rounded-[10px] border bg-card p-5" onSubmit={(event) => { event.preventDefault(); void createStudentRows(); }}>
              <p className="text-sm text-muted-foreground">{locale === "zh-CN" ? "尚未确定学生数量。填写人数后，将在此矩阵建立空白行；点击学生进入原作答详情填写姓名、学号和作答。" : "The student count is unknown. Enter it to create rows in this matrix, then open each student's details to enter their name, ID and answers."}</p>
              <div className="mt-4 flex flex-wrap items-end gap-3">
                <label className="grid gap-2 text-sm">{locale === "zh-CN" ? "学生人数" : "Number of students"}
                  <input id="student-count" type="number" min={1} max={500} step={1} required value={studentCount} onChange={(event) => setStudentCount(event.target.value)} disabled={manualMutation.isPending} className="h-10 w-36 rounded-md border bg-background px-3" />
                </label>
                <button type="submit" disabled={manualMutation.isPending || !Number.isInteger(Number(studentCount)) || Number(studentCount) < 1 || Number(studentCount) > 500} className="h-10 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground disabled:opacity-50">{manualMutation.isPending ? (locale === "zh-CN" ? "正在建立…" : "Creating…") : (locale === "zh-CN" ? "建立学生行" : "Create student rows")}</button>
              </div>
              {countError ? <p role="alert" className="mt-3 text-sm text-danger">{countError}</p> : null}
            </form>
          ) : students.length === 0 || questions.length === 0 ? (
            <section className="overflow-hidden rounded-[10px] border bg-card">
              <MatrixState
                title={t("submissionReviewEmptyTitle")}
                description={t("submissionReviewEmptyDescription")}
                href={canEditRows ? undefined : `/tasks/${taskId}/submissions/upload`}
                action={canEditRows ? undefined : t("submissionReviewUploadAgain")}
              />
              {addStudentButton ? <div className="flex items-center gap-3 border-t px-4 py-3"><span className="text-xs text-muted-foreground">{locale === "zh-CN" ? "0 位学生" : "0 students"}</span>{addStudentButton}</div> : null}
            </section>
          ) : (
            <>
              <MatrixQueueWorkspace
                matrix={(
                  <section className="h-[330px] min-w-0 overflow-hidden rounded-[10px] border bg-card" aria-label={t("submissionReviewMatrixLabel")}>
                    <SubmissionMatrix
                      students={selection.students}
                      questions={selection.questions}
                      taskId={taskId}
                      sources={taskQuery.data?.submission_sources}
                      returnSearch={returnSearch}
                      onDelete={canEditRows ? (student) => { setRowError(null); setDeletingStudent(student); } : undefined}
                      rowBusy={rowBusy}
                      columnSort={headerSort.current}
                      onSort={headerSort.toggle}
                      t={t}
                    />
                  </section>
                )}
                queue={(
                  <SubmissionReviewQueue confirmation={<ReviewConfirmButton locale={locale} all confirmed={!pendingReviews.length && !submissionReviewBlockers(taskQuery.data, locale).length} busy={batchProgress !== null} onClick={() => void confirmAll()} />} items={queueItems} t={t} />
                )}
              />

              <ReviewStageActions
                note={locale === "zh-CN" ? `显示 ${selection.students.length}/${students.length} 位学生 · ${selection.questions.length}/${questions.length} 道题` : `Showing ${selection.students.length}/${students.length} students · ${selection.questions.length}/${questions.length} questions`}
                leftAction={addStudentButton}

                back={{ href: `/tasks/${taskId}/students/${encodeURIComponent(Object.values(taskQuery.data?.student_data ?? {})[0]?.stu_id ?? "")}`, label: locale === "zh-CN" ? "查看作答详情" : "View Submission Details" }}
                next={{ label: t("submissionReviewEnterGradingSetup"), disabled: batchProgress !== null, onClick: () => {
                  const issues = submissionReviewBlockers(taskQuery.data, locale);
                  if (issues.length) setBlocked(issues);
                  else navigate(`/tasks/${taskId}/grading-setup`);
                } }}
              />
            </>
          )}
        </div>
      </section>
    </div>
  );
}


function SubmissionMatrix({
  students,
  sources = [],
  questions,
  taskId,
  returnSearch,
  onDelete,
  rowBusy,
  columnSort,
  onSort,
  t,
}: {
  students: StudentSubmission[];
  sources?: SubmissionSourceOutcome[];
  questions: SubmissionQuestion[];
  taskId: string;
  returnSearch: string;
  onDelete?: (student: StudentSubmission) => void;
  rowBusy?: boolean;
  columnSort: ColumnSort | null;
  onSort: (key: string) => void;
  t: (key: MessageKey) => string;
}) {
  const { locale } = useI18n();
  if (students.length === 0 || questions.length === 0) {
    return (
      <MatrixState
        title={t("submissionReviewNoMatches")}
        description={t("submissionReviewNoMatchesDescription")}
      />
    );
  }

  const studentIdLabel = t("submissionReviewColumnId");
  const studentNameLabel = t("submissionReviewColumnName");
  const identityLayout = getMatrixIdentityLayout({
    questionCount: questions.length,
    studentIds: students.map((student) => student.stu_id),
    studentNames: students.map((student) => student.stu_name || student.stu_id),
    studentIdLabel,
    studentNameLabel,
    reserveIdStatusIcon: students.some((student) => studentIdentityReviewBlocked(student, sources)),
  });
  const actionColumnWidth = onDelete ? 96 : MATRIX_ACTION_COLUMN_WIDTH;
  const tableMinWidth = Math.max(
    740,
    identityLayout.studentIdWidth
      + identityLayout.studentNameWidth
      + actionColumnWidth
      + questions.length * MATRIX_QUESTION_COLUMN_WIDTH,
  );

  return (
    <div className="h-full w-full overflow-auto overscroll-contain">
      <table
        className="w-full border-collapse text-left text-[13px]"
        style={{ minWidth: `${tableMinWidth}px` }}
      >
        <thead className="sticky top-0 z-20 bg-slate-100/95 text-[12px] font-semibold text-muted-foreground backdrop-blur-sm dark:bg-slate-800/95">
          <tr className="h-[42px] border-b">
            <SortableTableHead
              className="sm:sticky left-0 z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
              style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
             direction={directionFor(columnSort, "id")} onSort={() => onSort("id")}>{studentIdLabel}</SortableTableHead>
            <SortableTableHead
              className="sm:sticky z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
              style={{ left: identityLayout.studentIdWidth, width: identityLayout.studentNameWidth, minWidth: identityLayout.studentNameWidth, maxWidth: identityLayout.studentNameWidth }}
             direction={directionFor(columnSort, "name")} onSort={() => onSort("name")}>{studentNameLabel}</SortableTableHead>
            {questions.map((question) => (
              <SortableTableHead locale={locale} direction={directionFor(columnSort, `question:${question.id}`)} onSort={() => onSort(`question:${question.id}`)} label={question.label} key={question.id} className="w-[60px] min-w-[60px] max-w-[60px] px-1 text-center" title={question.type || question.label}>
                {question.label}
              </SortableTableHead>
            ))}
            <th className="px-3 text-right" style={{ width: actionColumnWidth }}>{t("submissionReviewColumnAction")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {students.map((student) => {
            const answers = answerMap(student);
            const entryQuestion = firstReviewQuestion(student, questions);
            const entryHref = entryQuestion
              ? studentReviewPath(taskId, student.stu_id, entryQuestion.id, returnSearch)
              : studentPath(taskId, student.stu_id);
            return (
              <tr key={student.stu_id} className="h-[52px] bg-card transition-colors hover:bg-muted/25">
                <td
                  className="sm:sticky left-0 z-10 whitespace-nowrap bg-card px-3 font-semibold text-foreground"
                  style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
                >
                  <Link
                    to={entryHref}
                    className="inline-flex items-center gap-2 whitespace-nowrap outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
                    title={student.stu_id}
                  >
                    {studentIdentityReviewBlocked(student, sources) ? (
                      <span className="h-2 w-2 shrink-0 rounded-full bg-red-500" aria-label={t("submissionReviewIdentityNeedsReview")} />
                    ) : null}
                    <span>{student.stu_id}</span>
                  </Link>
                </td>
                <td
                  className="sm:sticky z-10 whitespace-nowrap bg-card px-3 text-muted-foreground"
                  style={{ left: identityLayout.studentIdWidth, width: identityLayout.studentNameWidth, minWidth: identityLayout.studentNameWidth, maxWidth: identityLayout.studentNameWidth }}
                  title={student.stu_name || student.stu_id}
                >
                  {student.stu_name || student.stu_id}
                </td>
                {questions.map((question) => (
                  <td key={question.id} className="w-[60px] min-w-[60px] max-w-[60px] px-1 text-center">
                    <AnswerStatusLink
                      answer={answers.get(question.id)}
                      to={studentReviewPath(taskId, student.stu_id, question.id, returnSearch)}
                      t={t}
                    />
                  </td>
                ))}
                <td className="px-3 text-right" style={{ width: actionColumnWidth }}>
                  <div className="inline-flex items-center gap-1">
                    <MatrixViewLink to={entryHref} locale={locale} iconOnly />
                    {onDelete ? <button type="button" disabled={rowBusy} onClick={() => onDelete(student)} title={locale === "zh-CN" ? "删除" : "Delete"} aria-label={`${locale === "zh-CN" ? "删除学生" : "Delete student"} ${student.stu_id}`} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-red-50 hover:text-red-700 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"><Trash2 aria-hidden="true" className="h-4 w-4" /></button> : null}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AnswerStatusLink({
  answer,
  to,
  t,
}: {
  answer?: StudentAnswerInfo;
  to: string;
  t: (key: MessageKey) => string;
}) {
  const { locale } = useI18n();
  const state = getAnswerState(answer);
  const labels: Record<SubmissionAnswerState, MessageKey> = {
    recognized: "submissionReviewCellRecognized",
    reviewed: "submissionReviewCellReviewed",
    flagged: "submissionReviewCellFlagged",
    empty: "submissionReviewCellEmpty",
    missing: "submissionReviewCellMissing",
  };
  const stateLabel = state === "reviewed" ? (locale === "zh-CN" ? "已确认" : "Confirmed") : answerReviewBlocked(answer) ? (locale === "zh-CN" ? "需处理" : "Action required") : t(labels[state]);
  const attention = answerReviewAttention(answer);
  const label = attention === "blocking" && answer?.flag?.length ? `${stateLabel} · ${answer.flag.map((flag) => formatSubmissionFlag(flag, locale)).join(" · ")}` : stateLabel;
  const tone: MatrixStatusTone = state === "reviewed" ? "reviewed" : attention === "blocking" ? "error" : attention === "advisory" ? "warning" : "ok";

  return <MatrixStatusCell to={to} label={label} tone={tone} />;
}

function SubmissionReviewQueue({
  items,
  t,
  confirmation,
}: {
  confirmation: React.ReactNode;
  items: SubmissionQueueItem[];
  t: (key: MessageKey) => string;
}) {
  const { locale } = useI18n();
  return <ReviewQueuePanel locale={locale} confirmation={confirmation} items={items.map(item => ({
    key: item.key, title: `${item.studentName} · ${item.questionLabel}`, reason: item.reason || t(item.reasonKey),
    href: item.href, severity: item.severity,
  }))} />;
}

function ReviewMetric({
  label,
  value,
  detail,
  tone = "primary",
}: {
  label: string;
  value: string;
  detail: string;
  tone?: "primary" | "accent" | "warning" | "danger" | "neutral";
}) {
  return (
    <div className="min-h-[90px] rounded-[10px] border bg-card px-4 py-3.5 sm:px-5">
      <dt className="text-[12px] font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-2 flex items-baseline gap-2">
        <span
          className={cn(
            "text-[24px] font-bold leading-7 tracking-[-0.02em]",
            tone === "primary" && "text-primary",
            tone === "accent" && "text-teal-600 dark:text-teal-300",
            tone === "warning" && "text-amber-600 dark:text-amber-300",
            tone === "danger" && "text-red-500 dark:text-red-300",
            tone === "neutral" && "text-foreground",
          )}
        >
          {value}
        </span>
        <span className="text-[11px] text-muted-foreground">{detail}</span>
      </dd>
    </div>
  );
}

function MatrixState({
  title,
  description,
  action,
  onAction,
  href,
  busy = false,
}: {
  title: string;
  description?: string;
  action?: string;
  onAction?: () => void;
  href?: string;
  busy?: boolean;
}) {
  const actionClass = "mt-4 inline-flex h-9 items-center justify-center rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <div className="flex min-h-[300px] items-center justify-center px-6 py-10 text-center" role={busy ? "status" : undefined}>
      <div className="max-w-md">
        <AlertCircle aria-hidden="true" className={cn("mx-auto h-6 w-6 text-muted-foreground", busy && "animate-pulse text-primary")} />
        <p className="mt-3 text-sm font-semibold text-foreground">{title}</p>
        {description ? <p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p> : null}
        {action && onAction ? <button type="button" className={actionClass} onClick={onAction}>{action}</button> : null}
        {action && href ? <Link className={actionClass} to={href}>{action}</Link> : null}
      </div>
    </div>
  );
}

function formatPercent(ready: number, total: number) {
  if (total <= 0) return "—";
  return `${Math.round((ready / total) * 100)}%`;
}

function normalizeFilter(value: string | null): SubmissionReviewFilter {
  return value === "review" || value === "missing" || value === "identity" ? value : "all";
}

function normalizeSort(value: string | null): SubmissionReviewSort {
  return value === "student_name" || value === "attention" ? value : "student_id";
}

function studentPath(taskId: string, studentId: string) {
  return `/tasks/${encodeURIComponent(taskId)}/students/${encodeURIComponent(studentId)}`;
}

function studentReviewPath(taskId: string, studentId: string, questionId: string, returnSearch: string) {
  const params = new URLSearchParams({ question: questionId });
  if (returnSearch) params.set("returnParams", returnSearch);
  return `${studentPath(taskId, studentId)}?${params.toString()}`;
}

function identityReviewPath(taskId: string, studentId: string, returnSearch: string) {
  const params = new URLSearchParams({ identity: "edit" });
  if (returnSearch) params.set("returnParams", returnSearch);
  return `${studentPath(taskId, studentId)}?${params.toString()}`;
}

function firstReviewQuestion(student: StudentSubmission, questions: SubmissionQuestion[]) {
  const answers = answerMap(student);
  return questions.find((question) => getAnswerState(answers.get(question.id)) === "flagged")
    ?? questions[0]
    ?? null;
}

function buildSubmissionQueueItems(
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
  taskId: string,
  returnSearch: string,
  locale: "zh-CN" | "en-US",
  sources: SubmissionSourceOutcome[] = [],
): SubmissionQueueItem[] {
  const items: SubmissionQueueItem[] = [];
  const reasonKeys: Record<Exclude<SubmissionAnswerState, "recognized" | "reviewed">, MessageKey> = {
    flagged: "submissionReviewCellFlagged",
    empty: "submissionReviewCellEmpty",
    missing: "submissionReviewCellMissing",
  };

  for (const student of students) {
    if (studentIdentityReviewBlocked(student, sources)) {
      items.push({
        key: `${student.stu_id}:identity`,
        href: identityReviewPath(taskId, student.stu_id, returnSearch),
        studentId: student.stu_id,
        studentName: student.stu_name || student.stu_id,
        questionLabel: "ID",
        reasonKey: "submissionReviewQueueIdentity",
        severity: "blocking",
      });
    }

    const answers = answerMap(student);
    for (const question of questions) {
      const answer = answers.get(question.id);
      const attention = answerReviewAttention(answer);
      if (attention === "none") continue;
      items.push({
        key: `${student.stu_id}:${question.id}`,
        href: studentReviewPath(taskId, student.stu_id, question.id, returnSearch),
        studentId: student.stu_id,
        studentName: student.stu_name || student.stu_id,
        questionLabel: question.label,
        reasonKey: reasonKeys.flagged,
        severity: attention,
        reason: answerReviewLabels(answer, locale).join(" · "),
      });
    }
  }

  return items.sort((a, b) => Number(b.severity === "blocking") - Number(a.severity === "blocking"));
}
