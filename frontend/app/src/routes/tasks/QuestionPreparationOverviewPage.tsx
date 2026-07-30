import { ArrowDown, ArrowUp, ArrowUpDown, CheckCircle2, ChevronRight, Filter, Search, X } from "lucide-react";
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { useTask } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { isProgrammingProblem } from "@/lib/questionPreparation";
import { questionSearchAliases } from "@/lib/questionSearch";
import type { PreparationIssue, ProblemInfo } from "@/types";

type QuestionMatrixRow = {
  problem: ProblemInfo;
  issues: PreparationIssue[];
};

type OpenRiskRow = {
  problem: ProblemInfo;
  issue: PreparationIssue;
};

type MatrixSortKey = "number" | "type" | "attention";
type MatrixSortDirection = "asc" | "desc";
type MaterialField = "stem" | "answer" | "rubric" | "tests";

export function QuestionPreparationOverviewPage() {
  const { taskId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
  const urlQuery = searchParams.get("q") ?? "";
  const [query, setQuery] = useState(urlQuery);
  const composingRef = useRef(false);
  const pendingCompositionCommitRef = useRef<number | null>(null);
  const lastCommittedQueryRef = useRef(urlQuery);
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [sortKey, setSortKey] = useState<MatrixSortKey>("number");
  const [sortDirection, setSortDirection] = useState<MatrixSortDirection>("asc");
  const deferredQuery = useDeferredValue(urlQuery);

  useEffect(() => {
    lastCommittedQueryRef.current = urlQuery;
    if (!composingRef.current && pendingCompositionCommitRef.current === null) {
      setQuery((current) => current === urlQuery ? current : urlQuery);
    }
  }, [urlQuery]);

  useEffect(() => () => {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
    }
  }, []);

  const problems = useMemo(
    () => sortProblems(Object.values(taskQuery.data?.problem_data ?? {}), locale),
    [locale, taskQuery.data?.problem_data],
  );
  const allRisks = useMemo(() => collectRiskRows(problems), [problems]);
  const allRows = useMemo<QuestionMatrixRow[]>(() => problems.map((problem) => ({
    problem,
    issues: (problem.preparation_issues ?? []).filter((issue) => issue.status === "open"),
  })), [problems]);
  const availableTypes = useMemo(
    () => [...new Set(problems.map((problem) => problem.type || tx(locale, "未分类", "Uncategorized")))].sort((a, b) => a.localeCompare(b, locale)),
    [locale, problems],
  );
  const rows = useMemo(() => {
    const textFiltered = filterMatrixRows(allRows, deferredQuery, locale);
    const typeFiltered = selectedTypes.size
      ? textFiltered.filter((row) => selectedTypes.has(row.problem.type || tx(locale, "未分类", "Uncategorized")))
      : textFiltered;
    return sortMatrixRows(typeFiltered, sortKey, sortDirection, locale);
  }, [allRows, deferredQuery, locale, selectedTypes, sortDirection, sortKey]);
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

  function updateQuery(value: string) {
    if (lastCommittedQueryRef.current === value) return;
    lastCommittedQueryRef.current = value;
    const next = new URLSearchParams(searchParams);
    if (value.trim()) next.set("q", value);
    else next.delete("q");
    setSearchParams(next, { replace: true });
  }

  function flushComposition(input: HTMLInputElement) {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
      pendingCompositionCommitRef.current = null;
    }
    composingRef.current = false;
    const finalValue = input.value;
    setQuery(finalValue);
    updateQuery(finalValue);
  }

  function toggleSort(key: MatrixSortKey) {
    if (sortKey === key) {
      setSortDirection((current) => current === "asc" ? "desc" : "asc");
      return;
    }
    setSortKey(key);
    setSortDirection("asc");
  }

  function toggleType(type: string) {
    setSelectedTypes((current) => {
      const next = new Set(current);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  const firstQuestionId = problems[0]?.q_id;
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

        <label className="relative mt-4 block">
          <span className="sr-only">{tx(locale, "SmarTAI 智能筛选题目资料", "SmarTAI Smart filter for question materials")}</span>
          <Search aria-hidden="true" className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            inputMode="search"
            value={query}
            onCompositionStart={() => {
              if (pendingCompositionCommitRef.current !== null) {
                window.clearTimeout(pendingCompositionCommitRef.current);
                pendingCompositionCommitRef.current = null;
              }
              composingRef.current = true;
            }}
            onCompositionEnd={(event) => {
              const input = event.currentTarget;
              composingRef.current = false;
              setQuery(input.value);
              pendingCompositionCommitRef.current = window.setTimeout(() => {
                flushComposition(input);
              }, 0);
            }}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setQuery(value);
              if (
                !composingRef.current
                && pendingCompositionCommitRef.current === null
                && !(event.nativeEvent as InputEvent).isComposing
              ) {
                updateQuery(value);
              }
            }}
            onBlur={(event) => {
              if (composingRef.current || pendingCompositionCommitRef.current !== null) {
                flushComposition(event.currentTarget);
              }
            }}
            placeholder={tx(locale, "SmarTAI 智能搜索：题号、题型、资料状态或风险原因", "SmarTAI Smart Search: question, type, material status, or risk")}
            className="h-12 w-full rounded-[10px] border bg-card pl-11 pr-4 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
          />
        </label>

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
              availableTypes={availableTypes}
              selectedTypes={selectedTypes}
              onSort={toggleSort}
              onToggleType={toggleType}
              onClearTypes={() => setSelectedTypes(new Set())}
            />
          ) : <MatrixEmpty filtered={Boolean(query || selectedTypes.size)} locale={locale} />}
          <footer className="flex min-h-[58px] flex-col gap-2 border-t px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between xl:px-5">
            <p className="text-xs text-muted-foreground">{locale === "zh-CN"
              ? `显示 ${rows.length} / ${problems.length} 道题 · ${allRisks.length} 个开放风险`
              : `Showing ${rows.length} of ${problems.length} ${problems.length === 1 ? "question" : "questions"} · ${allRisks.length} open ${allRisks.length === 1 ? "risk" : "risks"}`}</p>
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

function QuestionMatrix({ rows, taskId, locale, sortKey, sortDirection, availableTypes, selectedTypes, onSort, onToggleType, onClearTypes }: {
  rows: QuestionMatrixRow[];
  taskId: string;
  locale: string;
  sortKey: MatrixSortKey;
  sortDirection: MatrixSortDirection;
  availableTypes: string[];
  selectedTypes: Set<string>;
  onSort: (key: MatrixSortKey) => void;
  onToggleType: (type: string) => void;
  onClearTypes: () => void;
}) {
  return (
    <div className="max-h-[calc(100vh-520px)] min-h-[280px] overflow-auto overscroll-contain">
      <table className="w-full min-w-[1080px] border-collapse text-left text-[13px]">
        <thead className="sticky top-0 z-10 bg-muted/95 text-[12px] font-semibold text-muted-foreground backdrop-blur-sm">
          <tr className="border-b">
            <SortableHeading className="w-[88px] px-5" label={tx(locale, "题号", "No.")} sortKey="number" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <th className="relative w-[140px] px-3 py-3" aria-sort={sortKey === "type" ? sortDirection === "asc" ? "ascending" : "descending" : "none"}>
              <div className="flex items-center gap-1">
                <SortButton label={tx(locale, "题型", "Type")} sortKey="type" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
                <details className="relative">
                  <summary aria-label={tx(locale, "筛选题型", "Filter types")} className={cn("flex h-7 w-7 cursor-pointer list-none items-center justify-center rounded-[5px] hover:bg-slate-200 dark:hover:bg-slate-700", selectedTypes.size && "bg-blue-100 text-primary dark:bg-blue-950/40")}><Filter aria-hidden="true" className="h-3.5 w-3.5" /></summary>
                  <div className="absolute left-0 top-8 z-30 w-56 rounded-[8px] border bg-card p-2 shadow-xl">
                    <div className="mb-1 flex items-center justify-between px-2 py-1">
                      <span className="text-xs font-semibold text-foreground">{tx(locale, "选择一个或多个题型", "Select one or more types")}</span>
                      {selectedTypes.size ? <button type="button" onClick={onClearTypes} className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted" aria-label={tx(locale, "清空题型筛选", "Clear type filter")}><X aria-hidden="true" className="h-3.5 w-3.5" /></button> : null}
                    </div>
                    {availableTypes.map((type) => <label key={type} className="flex cursor-pointer items-center gap-2 rounded-[6px] px-2 py-2 text-xs font-medium text-foreground hover:bg-muted"><input type="checkbox" checked={selectedTypes.has(type)} onChange={() => onToggleType(type)} className="h-4 w-4 accent-primary" />{type}</label>)}
                  </div>
                </details>
              </div>
            </th>
            <th className="w-[145px] px-3 py-3">{tx(locale, "题目", "Question")}</th>
            <th className="w-[145px] px-3 py-3">{tx(locale, "标答", "Reference Answer")}</th>
            <th className="w-[145px] px-3 py-3">{tx(locale, "评分标准", "Rubric")}</th>
            <th className="w-[145px] px-3 py-3">{tx(locale, "测试样例", "Tests")}</th>
            <SortableHeading className="w-[145px] px-3" label={tx(locale, "审核提示", "Attention")} sortKey="attention" activeKey={sortKey} direction={sortDirection} locale={locale} onSort={onSort} />
            <th className="w-[100px] px-5 py-3 text-right">{tx(locale, "操作", "Action")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map(({ problem, issues }) => (
            <tr key={problem.q_id} className="h-[64px] hover:bg-muted/30">
              <td className="px-5 py-3 font-semibold text-foreground">{problem.number || problem.q_id}</td>
              <td className="px-3 py-3 text-muted-foreground">{problem.type || tx(locale, "未分类", "Uncategorized")}</td>
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
  return <th className={cn("py-3", className)} aria-sort={activeKey === sortKey ? direction === "asc" ? "ascending" : "descending" : "none"}><SortButton label={label} sortKey={sortKey} activeKey={activeKey} direction={direction} locale={locale} onSort={onSort} /></th>;
}

function SortButton({ label, sortKey, activeKey, direction, locale, onSort }: { label: string; sortKey: MatrixSortKey; activeKey: MatrixSortKey; direction: MatrixSortDirection; locale: string; onSort: (key: MatrixSortKey) => void }) {
  const active = activeKey === sortKey;
  const Icon = active ? direction === "asc" ? ArrowUp : ArrowDown : ArrowUpDown;
  return <button type="button" onClick={() => onSort(sortKey)} className="inline-flex h-7 items-center gap-1 rounded-[5px] text-left font-semibold hover:text-foreground" aria-label={tx(locale, `按${label}排序`, `Sort by ${label}`)}>{label}<Icon aria-hidden="true" className={cn("h-3.5 w-3.5", active ? "text-primary" : "text-slate-400")} /></button>;
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
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{filtered ? tx(locale, "清空搜索或题型筛选后查看全部题目资料。", "Clear the search or type filter to see every question.") : tx(locale, "完成题目识别后，这里会显示全题资料矩阵。", "The full material matrix appears after recognition.")}</p>
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

function filterMatrixRows(rows: QuestionMatrixRow[], rawQuery: string, locale: string) {
  const query = rawQuery.trim().toLocaleLowerCase();
  if (!query) return rows;
  const tokens = query.split(/[\s,，;；]+/).filter(Boolean);
  return rows.filter(({ problem, issues }) => {
    const statuses = (["stem", "answer", "rubric", "tests"] as const).map((field) => getMaterialStatus(problem, field, locale).label);
    const sourceText = [
      problem.number,
      problem.q_id,
      problem.type,
      problem.stem,
      problem.reference_answer,
      problem.criterion,
      ...statuses,
      ...(issues.flatMap((issue) => [issue.field, issue.code, issueCodeLabel(issue.code, locale), ...(issue.source_ids ?? [])])),
    ].filter(Boolean).join(" ");
    const haystack = `${sourceText} ${questionSearchAliases(sourceText)}`.toLocaleLowerCase();
    return tokens.every((token) => haystack.includes(token));
  });
}

function sortMatrixRows(rows: QuestionMatrixRow[], key: MatrixSortKey, direction: MatrixSortDirection, locale: string) {
  const value = (row: QuestionMatrixRow) => {
    if (key === "number") return row.problem.number || row.problem.q_id;
    if (key === "type") return row.problem.type || "";
    return row.issues.length;
  };
  return [...rows].sort((left, right) => {
    const a = value(left);
    const b = value(right);
    const compared = typeof a === "number" && typeof b === "number"
      ? a - b
      : String(a).localeCompare(String(b), locale, { numeric: true });
    return direction === "asc" ? compared : -compared;
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
  };
  return locale === "zh-CN" ? labels[code][0] : labels[code][1];
}

function tx(locale: string, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}
