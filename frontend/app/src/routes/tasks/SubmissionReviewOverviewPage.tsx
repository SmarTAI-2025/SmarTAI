import { AlertCircle, CheckCircle2, ChevronRight, LoaderCircle, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { useTask } from "@/api/hooks/tasks";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { MatrixQueueWorkspace } from "@/components/tasks/MatrixQueueWorkspace";
import { MatrixStatusCell, type MatrixStatusTone } from "@/components/tasks/MatrixStatusCell";
import { TaskFilterFeedback } from "@/components/tasks/TaskFilterFeedback";
import { SortableTableHead, compareSortableValues, toggleColumnSort, type ColumnSort, type SortDirection } from "@/components/ui/SortableTableHead";
import { useTaskFilterIntent } from "@/hooks/useTaskFilterIntent";
import { getMatrixIdentityLayout, MATRIX_ACTION_COLUMN_WIDTH, MATRIX_QUESTION_COLUMN_WIDTH } from "@/components/tasks/matrixLayout";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { emptyFilterIntent, parsePreparationQuery, selectSubmissionIntent, submissionStateMatches } from "@/lib/preparationQuery";
import {
  answerMap,
  buildSubmissionQuestions,
  getAnswerState,
  getSubmissionReviewStats,
  studentNeedsAttention,
  type SubmissionAnswerState,
  type SubmissionQuestion,
  type SubmissionReviewFilter,
  type SubmissionReviewSelection,
} from "@/lib/submissionReview";
import { getTaskDestination, hasTaskReachedStep } from "@/lib/taskFlow";
import type { FilterIntentResult, StudentAnswerInfo, StudentSubmission } from "@/types";

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
}

type SubmissionSortKey = "id" | "name" | "coverage" | "review" | `question:${string}`;
type SubmissionSort = { key: SubmissionSortKey; direction: SortDirection };
const DEFAULT_SUBMISSION_SORT: SubmissionSort = { key: "id", direction: "asc" };

export function SubmissionReviewOverviewPage() {
  const { taskId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale, t } = useI18n();
  const taskQuery = useTask(taskId);
  const query = searchParams.get("q") ?? "";
  const latestSearchParamsRef = useRef(new URLSearchParams(searchParams));
  const queryComposingRef = useRef(false);
  const pendingCompositionCommitRef = useRef<number | null>(null);
  const lastCommittedQueryRef = useRef(query);
  const [queryDraft, setQueryDraft] = useState(query);
  const [tableSort, setTableSort] = useState<SubmissionSort | null>(null);
  const filter = normalizeFilter(searchParams.get("status"));
  const sortParam = searchParams.get("sort");
  const urlSort = normalizeSort(sortParam);

  const students = useMemo(
    () => Object.values(taskQuery.data?.student_data ?? {}),
    [taskQuery.data?.student_data],
  );
  const questions = useMemo(
    () => buildSubmissionQuestions(Object.values(taskQuery.data?.problem_data ?? {}), students),
    [students, taskQuery.data?.problem_data],
  );
  const stats = useMemo(() => getSubmissionReviewStats(students, questions), [questions, students]);
  const localIntent = useMemo(
    () => resolveSubmissionLocalIntent(query, students, questions),
    [query, questions, students],
  );
  const smartFilter = useTaskFilterIntent({
    taskId,
    query,
    surface: "submission_review",
    localIntent,
    resolveLocalIntent: (value) => resolveSubmissionLocalIntent(value, students, questions),
  });
  const intent = smartFilter.unrecognized ? null : smartFilter.intent;
  const naturalSort = intent ? intentToSubmissionSort(intent.sort) : null;
  const effectiveSort = tableSort ?? (sortParam ? urlSort : naturalSort ?? DEFAULT_SUBMISSION_SORT);
  const selection = useMemo(
    () => selectSubmissionOverview(students, questions, intent, filter, effectiveSort),
    [effectiveSort, filter, intent, questions, students],
  );

  useEffect(() => {
    lastCommittedQueryRef.current = query;
    if (!queryComposingRef.current && pendingCompositionCommitRef.current === null) {
      setQueryDraft((current) => current === query ? current : query);
    }
  }, [query]);

  useEffect(() => {
    latestSearchParamsRef.current = new URLSearchParams(searchParams);
  }, [searchParams]);

  useEffect(() => () => {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
    }
  }, []);

  if (taskQuery.isSuccess && taskId) {
    if (!hasTaskReachedStep(taskQuery.data, 4)) {
      return <Navigate replace to={getTaskDestination(taskQuery.data)} />;
    }
  }

  function setParam(key: string, value: string, defaultValue: string) {
    const next = new URLSearchParams(latestSearchParamsRef.current);
    if (!value || value === defaultValue) next.delete(key);
    else next.set(key, value);
    latestSearchParamsRef.current = next;
    setSearchParams(next, { replace: true });
  }

  function commitQuery(value: string) {
    if (lastCommittedQueryRef.current === value) return;
    lastCommittedQueryRef.current = value;
    setParam("q", value, "");
  }

  function applyQuery(value: string) {
    const normalized = value.trim();
    commitQuery(normalized);
    void smartFilter.apply(normalized);
  }

  function flushComposition(input: HTMLInputElement) {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
      pendingCompositionCommitRef.current = null;
    }
    queryComposingRef.current = false;
    const finalValue = input.value;
    setQueryDraft(finalValue);
    commitQuery(finalValue);
  }

  function sortByColumn(key: "id" | "name" | `question:${string}`) {
    smartFilter.cancel();
    const current: ColumnSort | null = tableSort ?? (sortParam ? { key: urlSort.key, direction: urlSort.direction } : null);
    const next = toggleColumnSort(current, key) as SubmissionSort;
    setTableSort(next);
    if (key === "id" || key === "name") {
      setParam("sort", `${next.key}_${next.direction}`, "id_asc");
      return;
    }
    setParam("sort", "", "");
  }

  const attentionStudent = selection.students.find((student) => studentNeedsAttention(student, selection.questions));
  const detailStudent = attentionStudent ?? selection.students[0];
  const detailQuestion = detailStudent
    ? firstReviewQuestion(detailStudent, selection.questions)
    : null;
  const returnSearch = searchParams.toString();
  const queueItems = taskId
    ? buildSubmissionQueueItems(selection.students, selection.questions, taskId, returnSearch)
    : [];

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="min-h-9 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {t("submissionReviewTitle")}
      </h1>
      <NewTaskStepper currentStep={4} />

      <section className="mt-[22px] min-w-0" aria-labelledby="submission-review-matrix">
        <h2 id="submission-review-matrix" className="sr-only">{t("submissionReviewMatrixLabel")}</h2>

        <dl className="grid min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 lg:gap-5">
          <ReviewMetric
            label={t("submissionReviewMetricIdentity")}
            value={formatPercent(stats.identityMatched, stats.students)}
            detail={`${stats.identityMatched}/${stats.students}`}
            tone={stats.identityAnomalies > 0 ? "warning" : "accent"}
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
            tone={stats.reviewCells > 0 ? "danger" : "accent"}
          />
          <ReviewMetric
            label={t("submissionReviewMetricIdentityIssues")}
            value={String(stats.identityAnomalies)}
            detail={locale === "zh-CN" ? t("submissionReviewMetricStudents") : stats.identityAnomalies === 1 ? "student" : "students"}
            tone={stats.identityAnomalies > 0 ? "warning" : "neutral"}
          />
        </dl>

        <form
          className="mt-6 min-w-0 rounded-[10px] border bg-card p-2.5 sm:flex sm:min-h-[52px] sm:items-center sm:gap-2.5 sm:p-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            applyQuery(queryDraft);
          }}
        >
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <SmarTAIMascot variant={smartFilter.pending ? "grading" : "thinking"} size="xs" />
            <label className="relative block min-w-0 flex-1">
              <span className="sr-only">{locale === "zh-CN" ? "Ask SmarTAI：筛选与排序学生作答" : "Ask SmarTAI: filter and sort student answers"}</span>
              <input
              type="search"
              value={queryDraft}
              onCompositionStart={() => {
                if (pendingCompositionCommitRef.current !== null) {
                  window.clearTimeout(pendingCompositionCommitRef.current);
                  pendingCompositionCommitRef.current = null;
                }
                queryComposingRef.current = true;
              }}
              onCompositionEnd={(event) => {
                const input = event.currentTarget;
                queryComposingRef.current = false;
                setQueryDraft(input.value);
                pendingCompositionCommitRef.current = window.setTimeout(() => {
                  flushComposition(input);
                }, 0);
              }}
              onChange={(event) => {
                const value = event.currentTarget.value;
                smartFilter.cancel();
                setQueryDraft(value);
                if (
                  !queryComposingRef.current
                  && pendingCompositionCommitRef.current === null
                  && !(event.nativeEvent as InputEvent).isComposing
                ) {
                  commitQuery(value);
                }
              }}
              onBlur={(event) => {
                if (queryComposingRef.current || pendingCompositionCommitRef.current !== null) {
                  flushComposition(event.currentTarget);
                }
              }}
              placeholder={locale === "zh-CN" ? "Ask SmarTAI：找出缺答作答，或按覆盖率排序" : "Ask SmarTAI: show missing answers, or sort by coverage"}
                className="h-10 w-full rounded-[7px] border-0 bg-slate-50 pl-3 pr-3 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary/20 dark:bg-slate-900/50"
              />
            </label>
          </div>
          <button type="submit" disabled={smartFilter.pending || !queryDraft.trim()} className="mt-2 inline-flex h-10 shrink-0 items-center justify-center gap-1.5 rounded-[7px] bg-primary px-3 text-xs font-semibold text-primary-foreground disabled:opacity-50 sm:mt-0">
            {smartFilter.pending ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : null}
            {locale === "zh-CN" ? "应用" : "Apply"}
          </button>
          <label className="mt-2 block shrink-0 sm:mt-0 sm:w-[170px]">
            <span className="sr-only">{t("submissionReviewStatusLabel")}</span>
            <select
              value={filter}
              onChange={(event) => setParam("status", event.target.value, "all")}
              className="h-10 w-full rounded-[7px] border bg-card px-3 text-[13px] text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
            >
              <option value="all">{t("submissionReviewStatusAll")}</option>
              <option value="review">{t("submissionReviewStatusReview")}</option>
              <option value="missing">{t("submissionReviewStatusMissing")}</option>
              <option value="identity">{t("submissionReviewStatusIdentity")}</option>
            </select>
          </label>
        </form>
        <TaskFilterFeedback filter={smartFilter} taskId={taskId} />
        <div className="mt-2 flex min-h-5 items-start justify-between gap-3 px-1">
          <p className="text-[11px] leading-5 text-muted-foreground">
            {t(EXPLANATION_KEYS[selection.explanation])}
            {selection.confidenceAlias ? ` ${t("submissionReviewConfidenceAliasHint")}` : ""}
          </p>
          {query || filter !== "all" || sortParam ? (
            <button
              type="button"
              onClick={() => {
                if (pendingCompositionCommitRef.current !== null) {
                  window.clearTimeout(pendingCompositionCommitRef.current);
                  pendingCompositionCommitRef.current = null;
                }
                queryComposingRef.current = false;
                lastCommittedQueryRef.current = "";
                smartFilter.cancel();
                setTableSort(null);
                latestSearchParamsRef.current = new URLSearchParams();
                setQueryDraft("");
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
          {taskQuery.isLoading ? (
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
          ) : students.length === 0 || questions.length === 0 ? (
            <section className="overflow-hidden rounded-[10px] border bg-card">
              <MatrixState
                title={t("submissionReviewEmptyTitle")}
                description={t("submissionReviewEmptyDescription")}
                href={`/tasks/${taskId}/submissions/upload`}
                action={t("submissionReviewUploadAgain")}
              />
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
                      returnSearch={returnSearch}
                      columnSort={effectiveSort}
                      onSort={sortByColumn}
                      t={t}
                    />
                  </section>
                )}
                queue={(
                  <SubmissionReviewQueue items={queueItems} t={t} />
                )}
              />

              <footer className="mt-4 flex min-h-[58px] flex-col gap-2 rounded-[10px] border bg-card px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between xl:px-5">
              <p className="text-xs text-muted-foreground">
                {locale === "zh-CN"
                  ? <>
                    {t("submissionReviewShowingPrefix")}{selection.students.length}
                    {t("submissionReviewShowingMiddle")}{students.length}
                    {t("submissionReviewShowingStudentsSuffix")} · {selection.questions.length}/{questions.length}
                    {t("submissionReviewShowingQuestionsSuffix")}
                  </>
                  : <>Showing {selection.students.length} of {students.length} {students.length === 1 ? "student" : "students"} · {selection.questions.length} of {questions.length} {questions.length === 1 ? "question" : "questions"}</>}
              </p>
              <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
                {detailStudent && detailQuestion ? (
                  <Link
                    to={studentReviewPath(taskId, detailStudent.stu_id, detailQuestion.id, returnSearch)}
                    className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring sm:w-auto"
                  >
                    {t(attentionStudent ? "submissionReviewOpenStudent" : "submissionReviewOpenDetails")}
                  </Link>
                ) : null}
                <Link
                  to={`/tasks/${taskId}/grading-setup`}
                  className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 sm:w-auto"
                >
                  {t("submissionReviewEnterGradingSetup")}
                  <ChevronRight aria-hidden="true" className="h-4 w-4" />
                </Link>
              </div>
              </footer>
            </>
          )}
        </div>
      </section>
    </div>
  );
}


function SubmissionMatrix({
  students,
  questions,
  taskId,
  returnSearch,
  columnSort,
  onSort,
  t,
}: {
  students: StudentSubmission[];
  questions: SubmissionQuestion[];
  taskId: string;
  returnSearch: string;
  columnSort: SubmissionSort | null;
  onSort: (key: "id" | "name" | `question:${string}`) => void;
  t: (key: MessageKey) => string;
}) {
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
    reserveIdStatusIcon: students.some((student) => student.identity_status === "needs_review"),
  });
  const tableMinWidth = Math.max(
    740,
    identityLayout.studentIdWidth
      + identityLayout.studentNameWidth
      + MATRIX_ACTION_COLUMN_WIDTH
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
              className="sticky left-0 z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
              style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
              direction={columnSort?.key === "id" ? columnSort.direction : null}
              onSort={() => onSort("id")}
              sortLabel={t("submissionReviewSortId")}
            >
              {studentIdLabel}
            </SortableTableHead>
            <SortableTableHead
              className="sticky z-30 whitespace-nowrap bg-slate-100/95 px-3 dark:bg-slate-800/95"
              style={{ left: identityLayout.studentIdWidth, width: identityLayout.studentNameWidth, minWidth: identityLayout.studentNameWidth, maxWidth: identityLayout.studentNameWidth }}
              direction={columnSort?.key === "name" ? columnSort.direction : null}
              onSort={() => onSort("name")}
              sortLabel={t("submissionReviewSortName")}
            >
              {studentNameLabel}
            </SortableTableHead>
            {questions.map((question) => (
              <SortableTableHead
                key={question.id}
                className="w-[60px] min-w-[60px] max-w-[60px] px-1 text-center"
                direction={columnSort?.key === `question:${question.id}` ? columnSort.direction : null}
                onSort={() => onSort(`question:${question.id}`)}
                sortLabel={question.type || question.label}
                title={question.type || question.label}
              >
                {question.label}
              </SortableTableHead>
            ))}
            <th className="w-[72px] px-3 text-right">{t("submissionReviewColumnAction")}</th>
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
                  className="sticky left-0 z-10 whitespace-nowrap bg-card px-3 font-semibold text-foreground"
                  style={{ width: identityLayout.studentIdWidth, minWidth: identityLayout.studentIdWidth, maxWidth: identityLayout.studentIdWidth }}
                >
                  <Link
                    to={entryHref}
                    className="inline-flex items-center gap-2 whitespace-nowrap outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
                    title={student.stu_id}
                  >
                    {student.identity_status === "needs_review" ? (
                      <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500" aria-label={t("submissionReviewIdentityNeedsReview")} />
                    ) : null}
                    <span>{student.stu_id}</span>
                  </Link>
                </td>
                <td
                  className="sticky z-10 whitespace-nowrap bg-card px-3 text-muted-foreground"
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
                <td className="w-[72px] px-3 text-right">
                  <Link
                    to={entryHref}
                    className="text-xs font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {t("submissionReviewOpen")}
                  </Link>
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
  const state = getAnswerState(answer);
  const labels: Record<SubmissionAnswerState, MessageKey> = {
    recognized: "submissionReviewCellRecognized",
    reviewed: "submissionReviewCellReviewed",
    flagged: "submissionReviewCellFlagged",
    empty: "submissionReviewCellEmpty",
    missing: "submissionReviewCellMissing",
  };
  const stateLabel = t(labels[state]);
  const label = answer?.flag?.length ? `${stateLabel} · ${answer.flag.join(" · ")}` : stateLabel;
  const tone: MatrixStatusTone = state === "recognized"
    ? "ok"
    : state === "reviewed"
      ? "reviewed"
      : state === "flagged"
        ? "warning"
        : "error";

  return <MatrixStatusCell to={to} label={label} tone={tone} />;
}

function SubmissionReviewQueue({
  items,
  t,
}: {
  items: SubmissionQueueItem[];
  t: (key: MessageKey) => string;
}) {
  return (
    <section className="h-[330px] overflow-hidden rounded-[10px] border bg-card px-4 py-5" aria-labelledby="submission-review-queue-title">
      <h2 id="submission-review-queue-title" className="text-[16px] font-bold leading-6 text-foreground">
        {t("submissionReviewQueueTitle")}
      </h2>
      {!items.length ? (
        <div className="flex h-[248px] flex-col items-center justify-center text-center">
          <CheckCircle2 aria-hidden="true" className="h-7 w-7 text-teal-500" />
          <p className="mt-3 max-w-[220px] text-[12px] leading-5 text-muted-foreground">
            {t("submissionReviewQueueEmpty")}
          </p>
        </div>
      ) : (
        <ol className="mt-3 h-[250px] space-y-1 overflow-y-auto overscroll-contain pr-1">
          {items.map((item) => (
            <li key={item.key}>
              <Link
                to={item.href}
                className="flex min-h-[54px] items-center gap-2 rounded-[8px] px-2 py-1.5 text-[12px] outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-semibold text-foreground" title={`${item.studentId} ${item.studentName}`}>
                    {item.studentName} · {item.questionLabel}
                  </span>
                  <span className="mt-0.5 block truncate text-muted-foreground">{t(item.reasonKey)}</span>
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

function normalizeSort(value: string | null): SubmissionSort {
  if (value === "student_name" || value === "name_asc") return { key: "name", direction: "asc" };
  if (value === "name_desc") return { key: "name", direction: "desc" };
  if (value === "id_desc") return { key: "id", direction: "desc" };
  if (value === "coverage_asc") return { key: "coverage", direction: "asc" };
  if (value === "coverage_desc") return { key: "coverage", direction: "desc" };
  if (value === "review_asc") return { key: "review", direction: "asc" };
  if (value === "attention" || value === "review_desc") return { key: "review", direction: "desc" };
  return DEFAULT_SUBMISSION_SORT;
}

function intentToSubmissionSort(sort: FilterIntentResult["sort"]): SubmissionSort | null {
  if (sort === "name_asc") return { key: "name", direction: "asc" };
  if (sort === "name_desc") return { key: "name", direction: "desc" };
  if (sort === "id_asc") return { key: "id", direction: "asc" };
  if (sort === "id_desc") return { key: "id", direction: "desc" };
  if (sort === "coverage_asc") return { key: "coverage", direction: "asc" };
  if (sort === "coverage_desc") return { key: "coverage", direction: "desc" };
  if (sort === "review_asc") return { key: "review", direction: "asc" };
  if (sort === "review_desc") return { key: "review", direction: "desc" };
  return null;
}

function resolveSubmissionLocalIntent(
  raw: string,
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
): FilterIntentResult | null {
  const parsed = parsePreparationQuery(raw, "submission_review");
  if (parsed) return parsed;
  const normalized = normalizeSearchTerm(raw);
  if (!normalized) return emptyFilterIntent();
  if (students.some((student) => [student.stu_id, student.stu_name].some((value) => normalizeSearchTerm(value) === normalized))) {
    return { ...emptyFilterIntent(), text_terms: [raw.trim()] };
  }
  if (questions.some((question) => [question.id, question.label].some((value) => normalizeSearchTerm(value) === normalized))) {
    return { ...emptyFilterIntent(), question_tokens: [raw.trim()] };
  }
  return null;
}

function selectSubmissionOverview(
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
  intent: FilterIntentResult | null,
  filter: SubmissionReviewFilter,
  sort: SubmissionSort,
): SubmissionReviewSelection {
  const interpreted = selectSubmissionIntent(students, questions, intent);
  const filteredStudents = interpreted.students.filter((student) => submissionStateMatches(student, interpreted.questions, filter === "all" ? null : filter));
  const sortedStudents = sortSubmissionStudents(filteredStudents, interpreted.questions, sort);
  const requestedStatus = intent?.submission_status;
  const explanation: SubmissionReviewSelection["explanation"] = !sortedStudents.length
    ? "no_match"
    : filter === "identity" || requestedStatus === "identity"
      ? "identity"
      : filter === "missing" || requestedStatus === "missing"
        ? "missing"
        : filter === "review" || requestedStatus === "review"
          ? "review"
          : intent?.question_tokens.length
            ? "question"
            : intent?.text_terms.length
              ? "student"
              : "all";
  return {
    students: sortedStudents,
    questions: interpreted.questions,
    explanation,
    confidenceAlias: false,
  };
}

function sortSubmissionStudents(
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
  sort: SubmissionSort,
) {
  const answerCount = (student: StudentSubmission) => questions.filter((question) => Boolean(answerMap(student).get(question.id)?.content?.trim())).length;
  const reviewCount = (student: StudentSubmission) => questions.filter((question) => !["recognized", "reviewed"].includes(getAnswerState(answerMap(student).get(question.id)))).length;
  const value = (student: StudentSubmission): string | number => {
    if (sort.key.startsWith("question:")) {
      const questionId = sort.key.slice("question:".length);
      return answerStateSeverity(getAnswerState(answerMap(student).get(questionId)));
    }
    if (sort.key === "name") return student.stu_name || student.stu_id;
    if (sort.key === "coverage") return answerCount(student);
    if (sort.key === "review") return reviewCount(student);
    return student.stu_id;
  };
  return [...students].sort((left, right) => (
    compareSortableValues(value(left), value(right), sort.direction)
    || compareSortableValues(left.stu_id, right.stu_id, "asc")
  ));
}

function answerStateSeverity(state: SubmissionAnswerState) {
  if (state === "missing") return 3;
  if (state === "empty") return 2;
  if (state === "flagged") return 1;
  return 0;
}

function normalizeSearchTerm(value: string | null | undefined) {
  return (value ?? "").trim().toLocaleLowerCase().replace(/\s+/g, " ");
}

function studentPath(taskId: string, studentId: string) {
  return `/tasks/${encodeURIComponent(taskId)}/students/${encodeURIComponent(studentId)}`;
}

function studentReviewPath(taskId: string, studentId: string, questionId: string, returnSearch: string) {
  const params = new URLSearchParams({ question: questionId });
  if (returnSearch) params.set("returnParams", returnSearch);
  return `${studentPath(taskId, studentId)}?${params.toString()}`;
}

function firstReviewQuestion(student: StudentSubmission, questions: SubmissionQuestion[]) {
  const answers = answerMap(student);
  return questions.find((question) => !["recognized", "reviewed"].includes(getAnswerState(answers.get(question.id))))
    ?? questions[0]
    ?? null;
}

function buildSubmissionQueueItems(
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
  taskId: string,
  returnSearch: string,
): SubmissionQueueItem[] {
  const items: SubmissionQueueItem[] = [];
  const reasonKeys: Record<Exclude<SubmissionAnswerState, "recognized" | "reviewed">, MessageKey> = {
    flagged: "submissionReviewCellFlagged",
    empty: "submissionReviewCellEmpty",
    missing: "submissionReviewCellMissing",
  };

  for (const student of students) {
    const firstQuestion = firstReviewQuestion(student, questions) ?? questions[0];
    if (student.identity_status === "needs_review" && firstQuestion) {
      items.push({
        key: `${student.stu_id}:identity`,
        href: studentReviewPath(taskId, student.stu_id, firstQuestion.id, returnSearch),
        studentId: student.stu_id,
        studentName: student.stu_name || student.stu_id,
        questionLabel: "ID",
        reasonKey: "submissionReviewQueueIdentity",
      });
    }

    const answers = answerMap(student);
    for (const question of questions) {
      const state = getAnswerState(answers.get(question.id));
      if (state === "recognized" || state === "reviewed") continue;
      items.push({
        key: `${student.stu_id}:${question.id}`,
        href: studentReviewPath(taskId, student.stu_id, question.id, returnSearch),
        studentId: student.stu_id,
        studentName: student.stu_name || student.stu_id,
        questionLabel: question.label,
        reasonKey: reasonKeys[state],
      });
    }
  }

  return items;
}
