import { groundedRows, hasGroundedOrder } from "@/lib/groundedAsk";
import { SortButton as HeaderSortButton, SortableTableHead, useColumnSort, sortColumnRows, directionFor, type ColumnSort } from "@/components/ui/SortableTableHead";
import { CheckCircle2, ChevronRight, Filter, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { useTask } from "@/api/hooks/tasks";
import { TaskQueryBar } from "@/components/tasks/AskQueryBar";
import { useTaskFilterIntent } from "@/hooks/useTaskFilterIntent";
import { resolvePreparationQuery, selectPreparationQuestions } from "@/lib/taskPreparationFilter";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { compareValues } from "@/lib/sortValues";
import { isProgrammingProblem } from "@/lib/questionPreparation";
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
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
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
  const allRisks = useMemo(() => collectRiskRows(problems), [problems]);
  const allRows = useMemo<QuestionMatrixRow[]>(() => problems.map((problem) => ({
    problem,
    issues: (problem.preparation_issues ?? []).filter((issue) => issue.status === "open"),
  })), [problems]);
  const rows = useMemo(() => {
    const selected = new Set(selectPreparationQuestions(problems, smartFilter.intent).map((problem) => problem.q_id));
    const textFiltered = allRows.filter((row) => selected.has(row.problem.q_id));
    if (hasGroundedOrder(smartFilter.intent) && !headerSort.current) return groundedRows(textFiltered, smartFilter.intent, "questions", r => r.problem.q_id);
    return sortMatrixRows(textFiltered, sortKey, sortDirection, locale);
  }, [allRows, problems, smartFilter.intent, locale, sortDirection, sortKey]);
  const metrics = useMemo(() => ({
    questions: new Set(allRisks.map((row) => row.problem.q_id)).size,
    lowConfidence: allRisks.filter((row) => row.issue.code === "low_confidence").length,
    conflicts: allRisks.filter((row) => ["source_conflict", "ai_source_conflict", "rubric_step_reference_conflict"].includes(row.issue.code)).length,
    anomalies: allRisks.filter((row) => ["parse_anomaly", "generation_failed", "invalid_test_case", "reference_solution_failed_case"].includes(row.issue.code)).length,
  }), [allRisks]);

  if (taskQuery.isSuccess && taskQuery.data.status === "draft") {
    return <Navigate replace to={`/tasks/${taskId}/upload/problems`} />;
  }
  if (taskQuery.isSuccess && taskQuery.data.status === "extracting_problems") {
    return <Navigate replace to={`/tasks/${taskId}/problems/progress`} />;
  }

  function toggleSort(key: MatrixSortKey) { headerSort.toggle(key); }

  const firstQuestionId = rows[0]?.problem.q_id;
  const totalMaxScore = problems.reduce((total, problem) => total + (problem.max_score ?? 10), 0);
  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {tx(locale, "题目资料总览", "Question Material Overview")}
      </h1>
      <NewTaskStepper currentStep={2} />

      <section className="mt-[22px]" aria-labelledby="risk-matrix-title">
        <h2 id="risk-matrix-title" className="sr-only">{tx(locale, "全部题目资料状态矩阵", "All question material status matrix")}</h2>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4 sm:gap-4">
          <RiskMetric label={tx(locale, "待关注题目", "Questions to Review")} value={metrics.questions} tone="primary" />
          <RiskMetric label={tx(locale, "低置信项", "Low Confidence")} value={metrics.lowConfidence} tone="warning" />
          <RiskMetric label={tx(locale, "来源冲突", "Source Conflicts")} value={metrics.conflicts} tone="danger" />
          <RiskMetric label={tx(locale, "解析异常", "Parse Anomalies")} value={metrics.anomalies} tone="accent" />
        </dl>

        <TaskQueryBar className="mt-4" filter={smartFilter} taskId={taskId} locale={locale}
          label={tx(locale, "Ask SmarTAI：题目资料", "Ask SmarTAI: question materials")}
          placeholder={tx(locale, "按满分升序，或找出缺少标答的题目", "Sort by maximum score, or find missing reference answers")} />

        <div className="mt-4 overflow-hidden rounded-[10px] border bg-card">
          {taskQuery.isLoading ? (
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
              sortKey={sortKey}
              sortDirection={sortDirection}
              onSort={toggleSort}
            />
          ) : <MatrixEmpty filtered={Boolean(query)} locale={locale} />}
          <footer className="flex min-h-[58px] flex-col gap-2 border-t px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between xl:px-5">
            <p className="text-xs text-muted-foreground">{locale === "zh-CN"
              ? `显示 ${rows.length} / ${problems.length} 道题 · 作业总分 ${formatScore(totalMaxScore)} · ${allRisks.length} 个开放风险`
              : `Showing ${rows.length} of ${problems.length} ${problems.length === 1 ? "question" : "questions"} · ${formatScore(totalMaxScore)} total points · ${allRisks.length} open ${allRisks.length === 1 ? "risk" : "risks"}`}</p>
            {taskId && firstQuestionId ? (
              <Link to={`/tasks/${taskId}/questions/${encodeURIComponent(firstQuestionId)}/content`} className="inline-flex h-9 items-center justify-center gap-1.5 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring">
                {tx(locale, "进入完整审核", "Open Full Review")}
                <ChevronRight aria-hidden="true" className="h-4 w-4" />
              </Link>
            ) : null}
          </footer>
        </div>
      </section>
    </div>
  );
}

function QuestionMatrix({ rows, taskId, locale, sortKey, sortDirection, onSort }: {
  rows: QuestionMatrixRow[];
  taskId: string;
  locale: string;
  sortKey: MatrixSortKey;
  sortDirection: MatrixSortDirection;
  onSort: (key: MatrixSortKey) => void;
}) {
  return (
    <div className="max-h-[calc(100vh-520px)] min-h-[280px] overflow-auto overscroll-contain">
      <table className="w-full min-w-[1160px] border-collapse text-left text-[13px]">
        <thead className="sticky top-0 z-10 bg-muted/95 text-[12px] font-semibold text-muted-foreground backdrop-blur-sm">
          <tr className="border-b">
            <SortableHeading className="w-[88px] px-5" label={tx(locale, "题号", "No.")} sortKey="number" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <th className="relative w-[140px] px-3 py-3" aria-sort={sortKey === "type" ? sortDirection === "asc" ? "ascending" : "descending" : "none"}>
              <div className="flex items-center gap-1">
                <SortButton label={tx(locale, "题型", "Type")} sortKey="type" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
              </div>
            </th>
            <SortableHeading className="w-[105px] px-3" label={tx(locale, "满分", "Max Score")} sortKey="max_score" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[145px] px-3" label={tx(locale, "题目", "Question")} sortKey="stem" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[145px] px-3" label={tx(locale, "标答", "Reference Answer")} sortKey="answer" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[145px] px-3" label={tx(locale, "评分标准", "Rubric")} sortKey="rubric" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[145px] px-3" label={tx(locale, "测试样例", "Tests")} sortKey="tests" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <SortableHeading className="w-[145px] px-3" label={tx(locale, "审核提示", "Attention")} sortKey="attention" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <th className="w-[100px] px-5 py-3 text-right">{tx(locale, "操作", "Action")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map(({ problem, issues }) => (
            <tr key={problem.q_id} className="h-[64px] hover:bg-muted/30">
              <td className="px-5 py-3 font-semibold text-foreground">{problem.number || problem.q_id}</td>
              <td className="px-3 py-3 text-muted-foreground">{problem.type || tx(locale, "未分类", "Uncategorized")}</td>
              <td className="px-3 py-3"><MaxScoreStatus problem={problem} locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="stem" locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="answer" locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="rubric" locale={locale} /></td>
              <td className="px-3 py-3"><MaterialStatus problem={problem} field="tests" locale={locale} /></td>
              <td className="px-3 py-3"><AttentionStatus issues={issues} locale={locale} /></td>
              <td className="px-5 py-3 text-right"><Link to={`/tasks/${taskId}/questions/${encodeURIComponent(problem.q_id)}/content#question-${encodeURIComponent(problem.q_id)}`} className="text-xs font-semibold text-primary hover:underline">{tx(locale, "审核", "Review")}</Link></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SortableHeading({ className, label, sortKey, activeKey, direction, locale, onSort }: { className: string; label: string; sortKey: MatrixSortKey; activeKey: MatrixSortKey; direction: MatrixSortDirection; locale: string; onSort: (key: MatrixSortKey) => void }) {
  return <SortableTableHead className={cn("py-3", className)} direction={activeKey === sortKey ? direction : null} onSort={() => onSort(sortKey)} locale={locale}>{label}</SortableTableHead>;
}

function SortButton({ label, sortKey, activeKey, direction, locale, onSort }: { label: string; sortKey: MatrixSortKey; activeKey: MatrixSortKey; direction: MatrixSortDirection; locale: string; onSort: (key: MatrixSortKey) => void }) {
  return <HeaderSortButton label={label} direction={activeKey === sortKey ? direction : null} onSort={() => onSort(sortKey)} locale={locale}>{label}</HeaderSortButton>;
}

function RiskMetric({ label, value, tone }: { label: string; value: number; tone: "primary" | "warning" | "danger" | "accent" }) {
  return (
    <div className="flex min-h-[112px] flex-col justify-center rounded-[10px] border bg-card px-5 py-4 sm:px-6">
      <dt className="order-2 mt-2 text-sm font-medium text-muted-foreground">{label}</dt>
      <dd className={cn("order-1 text-[30px] font-bold leading-9 tracking-[-0.02em]", tone === "primary" && "text-primary", tone === "warning" && "text-amber-600", tone === "danger" && "text-red-600", tone === "accent" && "text-teal-600")}>{value}</dd>
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
      title={sourceLabel}
      className={cn(
        "inline-flex min-w-[68px] items-center justify-center rounded-full px-2.5 py-1 text-xs font-semibold",
        needsReview
          ? "bg-amber-100 text-amber-700 dark:bg-amber-950/35 dark:text-amber-300"
          : "bg-blue-50 text-primary dark:bg-blue-950/35",
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
  const blocking = issues.some((issue) => issue.severity === "blocking");
  return <span title={issues.map((issue) => issueCodeLabel(issue.code, locale)).join("；")} className={cn("inline-flex min-w-[88px] items-center justify-center rounded-full px-3 py-1 text-xs font-semibold", blocking ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700")}>{tx(locale, `${issues.length} 项需核对`, `${issues.length} to review`)}</span>;
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
  const issues = (problem.preparation_issues ?? []).filter((issue) => issue.status === "open" && (issue.field === issueField || (field === "stem" && issue.field === "source")));
  if (issues.length) {
    const blocking = issues.some((issue) => issue.severity === "blocking");
    return {
      label: blocking ? tx(locale, "需处理", "Action needed") : tx(locale, "需核对", "Review"),
      detail: issues.map((issue) => issueCodeLabel(issue.code, locale)).join("；"),
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
  if (!valueReady) return { label: tx(locale, "待处理", "Pending"), detail: tx(locale, "本项资料尚未准备完成", "This material is not ready"), tone: "danger" };

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
  return problems.flatMap((problem) => (problem.preparation_issues ?? [])
    .filter((issue) => issue.status === "open")
    .map((issue) => ({ problem, issue })));
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

function issueCodeLabel(code: PreparationIssue["code"], locale: string) {
  const labels: Record<PreparationIssue["code"], [string, string]> = {
    low_confidence: ["匹配置信度较低，请与原文件核对", "Low-confidence match; compare with the source"],
    source_conflict: ["多份教师资料内容不一致", "Teacher sources disagree"],
    ai_source_conflict: ["SmarTAI 结果与原文件存在冲突", "SmarTAI output conflicts with the source"],
    ambiguous_question_match: ["无法唯一匹配到题号", "Question match is ambiguous"],
    unmapped_source_content: ["原文件中有内容尚未匹配", "Some source content is unmatched"],
    parse_anomaly: ["文件解析结果异常", "File parsing anomaly"],
    generation_failed: ["所需内容生成失败", "Required content generation failed"],
    rubric_step_reference_conflict: ["评分步骤与标答步骤未正确对应", "Rubric steps do not align with reference-answer steps"],
    invalid_test_case: ["测试样例结构无效", "Invalid test case structure"],
    reference_solution_failed_case: ["参考解未通过测试样例", "Reference solution failed a test"],
    default_max_score_requires_review: ["当前使用默认 10 分，请确认题目满分", "Default 10-point maximum; confirm the score"],
    max_score_not_found: ["未从每题分值说明中匹配到本题，已暂按 10 分", "No score matched this question; temporarily set to 10"],
  };
  return locale === "zh-CN" ? labels[code][0] : labels[code][1];
}

function formatScore(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function tx(locale: string, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}
