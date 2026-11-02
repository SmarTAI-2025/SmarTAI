import { questionIssueCodeLabel as issueCodeLabel } from "@/lib/reviewLabels";
import { getTaskDestination } from "@/lib/taskFlow";
import { ReviewQueue as ReviewQueuePanel } from "@/components/tasks/ReviewQueue";
import { questionIssueNeedsReview, questionReviewConfirmed, questionReviewIssues, questionReviewBlockers, recognitionIssueLabel, type ReviewBlocker } from "@/lib/reviewConfirmation";
import { MatrixViewLink } from "@/components/tasks/MatrixViewLink";
import { MatrixQueueWorkspace } from "@/components/tasks/MatrixQueueWorkspace";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { reviewActionClass, ReviewConfirmButton, ReviewBlockDialog, ReviewStageActions } from "@/components/tasks/ReviewConfirmation";
import { groundedRows, hasGroundedOrder } from "@/lib/groundedAsk";
import { SortButton as HeaderSortButton, SortableTableHead, useColumnSort, sortColumnRows, directionFor, type ColumnSort } from "@/components/ui/SortableTableHead";
import { CheckCircle2, ChevronRight, Filter, Plus, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAddTaskQuestion, useDeleteTaskQuestion, useManuallyCompleteQuestionPreparation, useTask } from "@/api/hooks/tasks";
import { useQuestionReview } from "@/hooks/useQuestionReview";
import { toast } from "sonner";
import { TaskQueryBar } from "@/components/tasks/AskQueryBar";
import { useTaskFilterIntent } from "@/hooks/useTaskFilterIntent";
import { resolvePreparationQuery, selectPreparationQuestions } from "@/lib/taskPreparationFilter";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { compareValues } from "@/lib/sortValues";
import { isProgrammingProblem } from "@/lib/questionPreparation";
import { questionTypeLabel } from "@/lib/reviewLabels";
import type { PreparationIssue, ProblemInfo } from "@/types";

type QuestionMatrixRow = {
  problem: ProblemInfo;
  issues: PreparationIssue[];
};

type OpenRiskRow = {
  problem: ProblemInfo;
  issue: PreparationIssue;
};

type MatrixSortKey = "number" | "type" | "attention" | "max_score" | "stem" | "answer" | "rubric" | "tests";
type MatrixSortDirection = "asc" | "desc";
type MaterialField = "stem" | "answer" | "rubric" | "tests";

export function QuestionPreparationOverviewPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const createRows = useManuallyCompleteQuestionPreparation();
  const addQuestion = useAddTaskQuestion();
  const deleteQuestion = useDeleteTaskQuestion();
  const [deletingQuestion, setDeletingQuestion] = useState<ProblemInfo | null>(null);
  const [rowError, setRowError] = useState(false);
  const rowBusy = addQuestion.isPending || deleteQuestion.isPending;
  const [questionCount, setQuestionCount] = useState("");
  const [countError, setCountError] = useState(false);
  const [nextBlockers, setNextBlockers] = useState<ReviewBlocker[]>([]);
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale } = useI18n();
  // Progress polls /state independently. Revalidate detail before its cached
  // running status can send the completed task straight back to progress.
  const taskQuery = useTask(taskId, { refetchOnMount: "always" });
  const readingCompletion = taskQuery.isFetching && ["draft", "extracting_problems"].includes(taskQuery.data?.status ?? "");
  const { confirm, confirming, updateProblem, failure, blocked, clearBlocked } = useQuestionReview(taskId ?? "", taskQuery.data?.workflow_revision, locale);
  const urlQuery = searchParams.get("q") ?? "";
  const query = urlQuery;
  const problems = useMemo(
    () => sortProblems(Object.values(taskQuery.data?.problem_data ?? {}), locale),
    [locale, taskQuery.data?.problem_data],
  );
  const smartFilter = useTaskFilterIntent({ taskId, surface: "question_preparation", resolveLocal: (value) => resolvePreparationQuery(problems, value) });
  const activeSort = searchParams.get("sort") ?? smartFilter.intent?.sort ?? "question";
  const naturalKey: MatrixSortKey = activeSort.startsWith("max_score") ? "max_score" : activeSort.startsWith("type") ? "type" : activeSort.startsWith("review") ? "attention" : "number";
  const headerSort = useColumnSort(["number", "type", "attention", "max_score", "stem", "answer", "rubric", "tests"],
    { key: naturalKey, direction: activeSort.endsWith("_desc") ? "desc" : "asc" }, smartFilter.cancel);
  const sortKey = (headerSort.current?.key ?? naturalKey) as MatrixSortKey;
  const sortDirection = headerSort.current?.direction ?? "asc";
  const preserveGroundedOrder = hasGroundedOrder(smartFilter.intent) && !headerSort.current;
  const allRisks = useMemo(() => collectRiskRows(problems), [problems]);
  const allRows = useMemo<QuestionMatrixRow[]>(() => problems.map((problem) => ({
    problem,
    issues: questionReviewIssues(problem),
  })), [problems]);
  const rows = useMemo(() => {
    const selected = new Set(selectPreparationQuestions(problems, smartFilter.intent).map((problem) => problem.q_id));
    const textFiltered = allRows.filter((row) => selected.has(row.problem.q_id));
    if (preserveGroundedOrder) return groundedRows(textFiltered, smartFilter.intent, "questions", r => r.problem.q_id);
    return sortMatrixRows(textFiltered, sortKey, sortDirection, locale);
  }, [allRows, problems, smartFilter.intent, locale, sortDirection, sortKey, preserveGroundedOrder]);
  const metrics = useMemo(() => ({
    questions: new Set(allRisks.map((row) => row.problem.q_id)).size,
    lowConfidence: allRisks.filter((row) => ["low_confidence", "recognition_partial", "recognition_needs_review"].includes(row.issue.code)).length,
    conflicts: allRisks.filter((row) => ["source_conflict", "ai_source_conflict", "rubric_step_reference_conflict"].includes(row.issue.code)).length,
    anomalies: allRisks.filter((row) => ["parse_anomaly", "generation_failed", "invalid_test_case", "reference_solution_failed_case"].includes(row.issue.code)).length,
  }), [allRisks]);

  if (taskId && taskQuery.data && !taskQuery.isFetching && (taskQuery.data.status === "problems_paused"
      || (taskQuery.data.status === "error" && taskQuery.data.last_failed_job_id === taskQuery.data.extract_job_id))) {
    return <Navigate replace to={getTaskDestination(taskQuery.data)} />;
  }
  if (taskQuery.isSuccess && !taskQuery.isFetching && taskQuery.data.status === "draft") {
    return <Navigate replace to={`/tasks/${taskId}/upload/problems`} />;
  }
  if (taskQuery.isSuccess && !taskQuery.isFetching && taskQuery.data.status === "extracting_problems") {
    return <Navigate replace to={`/tasks/${taskId}/problems/progress`} />;
  }

  function toggleSort(key: MatrixSortKey) { headerSort.toggle(key); }

  const canEditRows = taskQuery.data?.status === "problems_ready" && !taskQuery.data.manual_question_count_required;
  async function changeQuestionRow(problem?: ProblemInfo) {
    if (!taskId || !taskQuery.data || rowBusy) return;
    setRowError(false);
    try {
      const input = { taskId, expectedWorkflowRevision: taskQuery.data.workflow_revision };
      if (problem) await deleteQuestion.mutateAsync({ ...input, qId: problem.q_id });
      else {
        await addQuestion.mutateAsync(input);
        smartFilter.setQuery("");
      }
      setDeletingQuestion(null);
    } catch { setRowError(true); }
  }
  const addButton = canEditRows ? <button type="button" disabled={rowBusy} onClick={() => void changeQuestionRow()} className={reviewActionClass}><Plus aria-hidden="true" className="h-4 w-4" />{tx(locale, "添加题目", "Add question")}</button> : null;
  const firstQuestionId = rows[0]?.problem.q_id ?? problems[0]?.q_id;
  const totalMaxScore = problems.reduce((total, problem) => total + (problem.max_score ?? 10), 0);
  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {tx(locale, "题目资料总览", "Question Material Overview")}
      </h1>
      {deletingQuestion ? <UnsavedChangesDialog title={tx(locale, "删除题目？", "Delete question?")} description={tx(locale, `将从当前复核矩阵移除第 ${deletingQuestion.number || "—"} 题。`, `Remove question ${deletingQuestion.number || "—"} from the current review matrix.`)} stayLabel={tx(locale, "取消", "Cancel")} leaveLabel={tx(locale, "删除", "Delete")} saving={rowBusy} onStay={() => { if (!rowBusy) setDeletingQuestion(null); }} onLeave={() => void changeQuestionRow(deletingQuestion)}>
        {rowError ? <p role="alert" className="text-sm text-danger">{tx(locale, "修改失败，请刷新后重试。", "Could not update rows. Refresh and retry.")}</p> : null}
      </UnsavedChangesDialog> : null}
      {rowError && !deletingQuestion ? <p role="alert" className="mt-3 text-sm text-danger">{tx(locale, "修改失败，请刷新后重试。", "Could not update rows. Refresh and retry.")}</p> : null}
      <ReviewBlockDialog locale={locale} issues={nextBlockers.length ? nextBlockers : blocked} onClose={() => { clearBlocked(); setNextBlockers([]); }} />
      <NewTaskStepper currentStep={2} blockedFromStep={questionReviewBlockers(problems, taskId ?? "", locale).length ? 3 : undefined} lockedStepReason={tx(locale, "请先处理关键问题", "Resolve required items first")} onLockedStepActivate={() => setNextBlockers(questionReviewBlockers(problems, taskId ?? "", locale))} />

      <section className="mt-[22px]" aria-labelledby="risk-matrix-title">
        <h2 id="risk-matrix-title" className="sr-only">{tx(locale, "全部题目资料状态矩阵", "All question material status matrix")}</h2>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4 sm:gap-4">
          <RiskMetric label={tx(locale, "待关注题目", "Questions to Review")} value={metrics.questions} tone="primary" />
          <RiskMetric label={tx(locale, "识别不确定项", "Recognition Uncertainty")} value={metrics.lowConfidence} tone="warning" />
          <RiskMetric label={tx(locale, "来源冲突", "Source Conflicts")} value={metrics.conflicts} tone="danger" />
          <RiskMetric label={tx(locale, "解析异常", "Parse Anomalies")} value={metrics.anomalies} tone="accent" />
        </dl>

        <TaskQueryBar className="mt-4" filter={smartFilter} taskId={taskId} locale={locale}
          label={tx(locale, "Ask SmarTAI：题目资料", "Ask SmarTAI: question materials")}
          placeholder={tx(locale, "按满分升序，或找出缺少标答的题目", "Sort by maximum score, or find missing reference answers")} />

        {failure ? <div role="alert" className="mt-4 rounded-lg border border-amber-300 p-4 text-sm">
          <p>{tx(locale, `已确认 ${failure.completed} 道题；第 ${failure.problem.number || "—"} 题未能保存，可能内容已更新或任务正在处理。请重新加载并核对后重试。`, `${failure.completed} questions confirmed; question ${failure.problem.number || "—"} could not be saved. Content may have changed or the task may be busy. Reload and review before retrying.`)}</p>
          <button type="button" onClick={() => void taskQuery.refetch()} className="mt-2 mr-4 font-semibold text-primary">{tx(locale, "重新加载", "Reload")}</button>
          <Link to={`/tasks/${taskId}/questions/${encodeURIComponent(failure.problem.q_id)}/content#question-${encodeURIComponent(failure.problem.q_id)}`} className="mt-2 font-semibold text-primary">{tx(locale, "前往该题", "Go to question")}</Link>
        </div> : null}

        <MatrixQueueWorkspace className="mt-4" queue={<QuestionReviewQueue confirmation={<ReviewConfirmButton locale={locale} all confirmed={problems.every(questionReviewConfirmed)} busy={confirming || updateProblem.isPending} onClick={async () => {
              if (await confirm(problems.filter(problem => questionReviewIssues(problem).length > 0 && !questionReviewIssues(problem).some(issue => issue.severity === "blocking") && rows.some(row => row.problem.q_id === problem.q_id)))) toast.success(tx(locale, "题目资料已确认。", "Question materials confirmed."));
            }} />} risks={allRisks.filter(row => rows.some(item => item.problem.q_id === row.problem.q_id))} taskId={taskId ?? ""} locale={locale} />} matrix={<div className="overflow-hidden rounded-[10px] border bg-card">
          {taskQuery.isLoading || readingCompletion ? (
            <div className="min-h-[300px] animate-pulse bg-muted/20" aria-busy="true" />
          ) : taskQuery.isError ? (
            <div className="flex min-h-[300px] flex-col items-center justify-center px-5 text-center">
              <p className="text-sm font-semibold text-foreground">{tx(locale, "无法读取题目资料状态", "Question material status could not be loaded")}</p>
              <button type="button" onClick={() => void taskQuery.refetch()} className="mt-3 h-9 rounded-[7px] border px-4 text-sm font-semibold hover:bg-muted">{tx(locale, "重新加载", "Reload")}</button>
            </div>
          ) : rows.length ? (
            <QuestionMatrix
              rows={rows}
              taskId={taskId ?? ""}
              locale={locale}
              sortKey={preserveGroundedOrder ? undefined : sortKey}
              sortDirection={sortDirection}
              onSort={toggleSort}
              onDelete={canEditRows ? setDeletingQuestion : undefined}
              busy={rowBusy}
            />
          ) : !problems.length && taskQuery.data?.manual_question_count_required && taskQuery.data.extract_job_id ? (
            <form className="p-5" onSubmit={async event => {
              event.preventDefault();
              if (!taskId || !taskQuery.data?.extract_job_id || createRows.isPending) return;
              setCountError(false);
              try {
                await createRows.mutateAsync({ taskId, jobId: taskQuery.data.extract_job_id,
                  expectedWorkflowRevision: taskQuery.data.workflow_revision, questionCount: Number(questionCount) });
              } catch { setCountError(true); }
            }}>
              <p className="mb-3 text-sm text-muted-foreground">{tx(locale, "尚未识别出题目数量。填写题数建立空白行，再进入详情填写题号和内容。", "The question count is unknown. Create blank rows, then enter question numbers and content in the existing details.")}</p>
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-sm font-semibold">{tx(locale, "题数", "Question count")}<input id="question-count" aria-label={tx(locale, "题数", "Question count")} type="number" min={1} max={500} step={1} required value={questionCount} onChange={event => setQuestionCount(event.target.value)} className="ml-3 h-9 w-24 rounded-md border bg-background px-3" /></label>
                <button type="submit" disabled={createRows.isPending || !Number.isInteger(Number(questionCount)) || Number(questionCount) < 1 || Number(questionCount) > 500} className="h-9 rounded-md bg-primary px-4 text-sm font-semibold text-primary-foreground disabled:opacity-45">{tx(locale, "建立题目行", "Create question rows")}</button>
              </div>
              {countError ? <p role="alert" className="mt-3 text-sm text-danger">{tx(locale, "未能建立题目行，请刷新后重试。", "Could not create question rows. Refresh and try again.")}</p> : null}
            </form>
          ) : <MatrixEmpty filtered={Boolean(query)} locale={locale} />}
        </div>} />
          {taskId ? <ReviewStageActions
            leftAction={addButton}
            note={locale === "zh-CN"
              ? `显示 ${rows.length} / ${problems.length} 道题 · 作业总分 ${formatScore(totalMaxScore)} · ${allRisks.length} 个复核项`
              : `Showing ${rows.length} of ${problems.length} questions · ${formatScore(totalMaxScore)} total points · ${allRisks.length} review items`}

            back={{ disabled: !firstQuestionId, href: firstQuestionId ? `/tasks/${taskId}/questions/${encodeURIComponent(firstQuestionId)}/content` : undefined, label: tx(locale, "查看题目详情", "View Question Details") }}
            next={{ label: tx(locale, "进入上传作答", "Continue to Upload Submissions"), disabled: confirming || updateProblem.isPending, onClick: () => {
              const issues = questionReviewBlockers(problems, taskId, locale);
              if (issues.length) setNextBlockers(issues);
              else navigate(`/tasks/${taskId}/submissions/upload`);
            } }}
          /> : null}
      </section>
    </div>
  );
}

function QuestionReviewQueue({ risks, taskId, locale, confirmation }: { confirmation: React.ReactNode; risks: OpenRiskRow[]; taskId: string; locale: string }) {
  const grouped = new Map<string, OpenRiskRow[]>();
  for (const row of risks) grouped.set(row.problem.q_id, [...(grouped.get(row.problem.q_id) ?? []), row]);
  return <ReviewQueuePanel locale={locale} confirmation={confirmation} items={[...grouped.values()].map(rows => {
    const { problem } = rows[0];
    const blocking = rows.some(row => row.issue.severity === "blocking");
    return { key: problem.q_id, title: problem.number || "—",
      severity: blocking ? "blocking" : "advisory",
      reason: [...new Set(rows.map(({ issue }) => ["recognition_partial", "recognition_needs_review"].includes(issue.code) && issue.severity === "blocking"
        ? tx(locale, "题目识别不完整，请补齐", "Question recognition incomplete; complete the missing content")
        : recognitionIssueLabel(issue, locale) ?? issueCodeLabel(issue.code, locale)))].join("；"),
      href: `/tasks/${taskId}/questions/${encodeURIComponent(problem.q_id)}/content#question-${encodeURIComponent(problem.q_id)}` };
  })} />;
}

function QuestionMatrix({ rows, taskId, locale, sortKey, sortDirection, onSort, onDelete, busy }: {
  rows: QuestionMatrixRow[];
  onDelete?: (problem: ProblemInfo) => void;
  busy?: boolean;
  taskId: string;
  locale: string;
  sortKey: MatrixSortKey | undefined;
  sortDirection: MatrixSortDirection;
  onSort: (key: MatrixSortKey) => void;
}) {
  return (
    <div className="h-[330px] overflow-auto overscroll-contain">
      <table className="w-full min-w-[860px] border-collapse text-left text-[13px]">
        <thead className="sticky top-0 z-10 bg-muted/95 text-[12px] font-semibold text-muted-foreground backdrop-blur-sm">
          <tr className="border-b">
            <SortableHeading className="w-[68px] px-3" label={tx(locale, "题号", "No.")} sortKey="number" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <th className="relative w-[100px] px-3 py-3" aria-sort={sortKey === "type" ? sortDirection === "asc" ? "ascending" : "descending" : "none"}>
              <div className="flex items-center gap-1">
                <SortButton label={tx(locale, "题型", "Type")} sortKey="type" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
              </div>
            </th>
            <SortableHeading className="w-[85px] px-3" label={tx(locale, "满分", "Max Score")} sortKey="max_score" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[110px] px-3" label={tx(locale, "题目", "Question")} sortKey="stem" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[110px] px-3" label={tx(locale, "标答", "Reference Answer")} sortKey="answer" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[110px] px-3" label={tx(locale, "评分标准", "Rubric")} sortKey="rubric" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[110px] px-3" label={tx(locale, "测试样例", "Tests")} sortKey="tests" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[110px] px-3" label={tx(locale, "审核提示", "Attention")} sortKey="attention" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <th className="w-[100px] px-5 py-3 text-right">{tx(locale, "操作", "Action")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map(({ problem, issues }) => (
            <tr key={problem.q_id} className="h-[64px] hover:bg-muted/30">
              <td className="px-5 py-3 font-semibold text-foreground">{problem.number || "—"}</td>
              <td className="px-3 py-3 text-muted-foreground">{questionTypeLabel(problem.type, locale)}</td>
              <td className="px-3 py-3"><MaxScoreStatus problem={problem} locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="stem" locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="answer" locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="rubric" locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="tests" locale={locale} /></td>
              <td className="px-3 py-3"><AttentionStatus issues={issues} locale={locale} /></td>
              <td className="px-5 py-3 text-right"><div className="flex items-center justify-end gap-1">
                <MatrixViewLink iconOnly to={`/tasks/${taskId}/questions/${encodeURIComponent(problem.q_id)}/content#question-${encodeURIComponent(problem.q_id)}`} locale={locale} />
                {onDelete ? <button type="button" title={tx(locale, "删除", "Delete")} aria-label={tx(locale, "删除", "Delete")} disabled={busy} onClick={() => onDelete(problem)} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-danger/10 hover:text-danger disabled:opacity-50"><Trash2 aria-hidden="true" className="h-4 w-4" /></button> : null}
              </div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SortableHeading({ className, label, sortKey, activeKey, direction, locale, onSort }: { className: string; label: string; sortKey: MatrixSortKey; activeKey: MatrixSortKey | undefined; direction: MatrixSortDirection; locale: string; onSort: (key: MatrixSortKey) => void }) {
  return <SortableTableHead className={cn("py-3", className)} direction={activeKey === sortKey ? direction : null} onSort={() => onSort(sortKey)} locale={locale}>{label}</SortableTableHead>;
}

function SortButton({ label, sortKey, activeKey, direction, locale, onSort }: { label: string; sortKey: MatrixSortKey; activeKey: MatrixSortKey | undefined; direction: MatrixSortDirection; locale: string; onSort: (key: MatrixSortKey) => void }) {
  return <HeaderSortButton label={label} direction={activeKey === sortKey ? direction : null} onSort={() => onSort(sortKey)} locale={locale}>{label}</HeaderSortButton>;
}

function RiskMetric({ label, value, tone }: { label: string; value: number; tone: "primary" | "warning" | "danger" | "accent" }) {
  return (
    <div className="flex min-h-[112px] flex-col justify-center rounded-[10px] border bg-card px-5 py-4 sm:px-6">
      <dt className="order-2 mt-2 text-sm font-medium text-muted-foreground">{label}</dt>
      <dd className={cn("order-1 text-[30px] font-bold leading-9 tracking-[-0.02em]", tone === "primary" && "text-primary", tone === "warning" && (value > 0 ? "text-amber-600" : "text-teal-600"), tone === "danger" && (value > 0 ? "text-red-600" : "text-teal-600"), tone === "accent" && "text-teal-600")}>{value}</dd>
    </div>
  );
}

function MaterialStatus({ problem, field, locale }: { problem: ProblemInfo; field: MaterialField; locale: string }) {
  const status = getMaterialStatus(problem, field, locale);
  return (
    <span
      title={status.detail}
      className={cn(
        "inline-flex min-w-[82px] items-center justify-center gap-1 rounded-full px-3 py-1 text-xs font-semibold",
        status.tone === "success" && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300",
        status.tone === "warning" && "bg-amber-100 text-amber-700 dark:bg-amber-950/35 dark:text-amber-300",
        status.tone === "danger" && "bg-red-100 text-red-700 dark:bg-red-950/35 dark:text-red-300",
        status.tone === "neutral" && "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-300",
      )}
    >
      {status.tone === "success" ? <CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5" /> : null}
      {status.label}
    </span>
  );
}

function MaxScoreStatus({ problem, locale }: { problem: ProblemInfo; locale: string }) {
  const needsReview = problem.max_score_review_status !== "confirmed";
  const source = problem.max_score_source ?? "legacy";
  const sourceLabel = {
    default_10: tx(locale, "系统默认，需确认", "System default; confirm it"),
    uniform: tx(locale, "统一设置", "Uniform setting"),
    per_question_text: tx(locale, "按描述识别，需确认", "Interpreted from your note; confirm it"),
    teacher_edited: tx(locale, "教师已修改", "Teacher edited"),
    legacy: tx(locale, "历史数据，需确认", "Legacy data; confirm it"),
  }[source];
  return (
    <span
      title={needsReview ? sourceLabel : tx(locale, "满分已确认", "Maximum score confirmed")}
      className={cn(
        "inline-flex min-w-[68px] items-center justify-center rounded-full px-2.5 py-1 text-xs font-semibold",
        problem.rubric_point_summary?.is_valid === false || (problem.preparation_issues ?? []).some(issue => issue.field === "max_score" && issue.severity === "blocking" && questionIssueNeedsReview(issue))
          ? "bg-red-100 text-red-700"
          : (problem.preparation_issues ?? []).some(issue => issue.field === "max_score" && questionIssueNeedsReview(issue)) ? "bg-amber-100 text-amber-800"
          : "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/35",
      )}
    >
      {formatScore(problem.max_score ?? 10)} {tx(locale, "分", "pts")}
    </span>
  );
}

function AttentionStatus({ issues, locale }: { issues: PreparationIssue[]; locale: string }) {
  if (!issues.length) {
    return <span className="inline-flex min-w-[88px] items-center justify-center gap-1 rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"><CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "状态正常", "Ready")}</span>;
  }
  return <span tabIndex={0} title={issues.map((issue) => (recognitionIssueLabel(issue, locale) ?? issueCodeLabel(issue.code, locale))).join("；")} className={cn("inline-flex min-w-[88px] items-center justify-center rounded-full px-3 py-1 text-xs font-semibold", issues.some(issue => issue.severity === "blocking") ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-800")}>{tx(locale, `${issues.length} 项待复核`, `${issues.length} to review`)}</span>;
}

function MatrixEmpty({ filtered, locale }: { filtered: boolean; locale: string }) {
  return (
    <div className="flex min-h-[220px] flex-col items-center justify-center px-5 text-center">
      <p className="text-sm font-semibold text-foreground">{filtered ? tx(locale, "没有匹配的题目", "No matching questions") : tx(locale, "尚未识别到题目", "No questions recognized yet")}</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{filtered ? tx(locale, "清空查询后查看全部题目资料。", "Clear the query to see every question.") : tx(locale, "完成题目识别后，这里会显示全题资料矩阵。", "The full material matrix appears after recognition.")}</p>
    </div>
  );
}

function getMaterialStatus(problem: ProblemInfo, field: MaterialField, locale: string): { label: string; detail: string; tone: "success" | "warning" | "danger" | "neutral" } {
  const issueField = field === "tests" ? "programming_tests" : field;
  const issues = questionReviewIssues(problem).filter(issue => issue.field === issueField || (field === "stem" && issue.field === "source"));
  if (issues.length) {
    const blocking = issues.some((issue) => issue.severity === "blocking");
    return {
      label: blocking ? tx(locale, "需处理", "Action needed") : tx(locale, "待复核", "Needs review"),
      detail: issues.map((issue) => (recognitionIssueLabel(issue, locale) ?? issueCodeLabel(issue.code, locale))).join("；"),
      tone: blocking ? "danger" : "warning",
    };
  }

  if (field === "tests" && !isProgrammingProblem(problem)) {
    return { label: tx(locale, "不适用", "N/A"), detail: tx(locale, "非编程题无需测试样例", "Test cases are not required for non-programming questions"), tone: "neutral" };
  }

  const valueReady = field === "stem"
    ? Boolean(problem.stem?.trim())
    : field === "answer"
      ? Boolean(problem.reference_answer?.trim())
      : field === "rubric"
        ? Boolean(problem.criterion?.trim())
        : Boolean(problem.test_cases?.length);
  if (!valueReady) return { label: problem.review_status === "confirmed" ? tx(locale, "已确认", "Confirmed") : tx(locale, "建议处理", "Suggested review"), detail: tx(locale, "本项资料尚未提供", "This material was not provided"), tone: problem.review_status === "confirmed" ? "success" : "warning" };

  if (field === "stem") return { label: tx(locale, "已识别", "Recognized"), detail: tx(locale, "题目正文已识别", "Question text recognized"), tone: "success" };
  const provenanceKey = field === "answer" ? "reference_answer" : field === "rubric" ? "criterion" : "test_cases";
  const material = problem.material_provenance?.[provenanceKey];
  const generated = problem.ai_completion_provenance?.[provenanceKey];
  if (material) return { label: tx(locale, "已识别", "Recognized"), detail: material.source_filename || tx(locale, "来自教师资料", "From teacher material"), tone: "success" };
  if (generated) return { label: tx(locale, "已生成", "Generated"), detail: tx(locale, "由 SmarTAI 生成并已准备", "Generated by SmarTAI and ready"), tone: "success" };
  if (field === "tests" && problem.test_cases?.every((item) => item.source === "llm_generated")) return { label: tx(locale, "已生成", "Generated"), detail: tx(locale, "测试样例由 SmarTAI 生成", "Test cases generated by SmarTAI"), tone: "success" };
  return { label: tx(locale, "已准备", "Ready"), detail: tx(locale, "资料已准备，可进入完整审核", "Material is ready for full review"), tone: "success" };
}

function collectRiskRows(problems: ProblemInfo[]): OpenRiskRow[] {
  return problems.flatMap(problem => questionReviewIssues(problem).map(issue => ({ problem, issue })))
    .sort((a, b) => Number(b.issue.severity === "blocking") - Number(a.issue.severity === "blocking"));
}

function sortMatrixRows(rows: QuestionMatrixRow[], key: MatrixSortKey, direction: MatrixSortDirection, locale: string) {
  const value = (row: QuestionMatrixRow) => {
    if (key === "number") return row.problem.number || row.problem.q_id;
    if (key === "type") return row.problem.type || "";
    if (key === "max_score") return row.problem.max_score;
    if (["stem", "answer", "rubric", "tests"].includes(key)) {
      const status = getMaterialStatus(row.problem, key as MaterialField, locale);
      return status.tone === "neutral" ? null : status.tone === "success" ? 0 : status.tone === "warning" ? 1 : 2;
    }
    return row.issues.length;
  };
  return [...rows].sort((left, right) => {
    const a = value(left);
    const b = value(right);
    return compareValues(a, b, direction);
  });
}

function sortProblems(problems: ProblemInfo[], locale: string) {
  return [...problems].sort((a, b) => (a.number || a.q_id).localeCompare(b.number || b.q_id, locale, { numeric: true }));
}

function formatScore(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function tx(locale: string, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}
