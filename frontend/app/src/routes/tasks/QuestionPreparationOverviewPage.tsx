import { CheckCircle2, ChevronRight, Filter, LoaderCircle, X } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { useAnalyticsFilterIntent } from "@/api/hooks/analytics";
import { useTask } from "@/api/hooks/tasks";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { SortableHeaderButton, SortableTableHead, type TableSortDirection } from "@/components/ui/SortableTableHead";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { isProgrammingProblem } from "@/lib/questionPreparation";
import { questionSearchAliases } from "@/lib/questionSearch";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import type { FilterIntentResult, PreparationIssue, ProblemInfo } from "@/types";

type QuestionMatrixRow = {
  problem: ProblemInfo;
  issues: PreparationIssue[];
};

type OpenRiskRow = {
  problem: ProblemInfo;
  issue: PreparationIssue;
};

type MatrixSortKey = "number" | "type" | "max_score" | MaterialField | "attention";
type MatrixSortDirection = "asc" | "desc";
type MaterialField = "stem" | "answer" | "rubric" | "tests";

interface SemanticCondition {
  id: string;
  label: string;
  source: string;
}

interface QuestionPreparationPlan {
  questionTokens: string[];
  types: string[];
  minMaxScore: number | null;
  maxMaxScore: number | null;
  preparationStatus: NonNullable<FilterIntentResult["preparation_status"]> | null;
  materialField: MaterialField | null;
  materialStatus: NonNullable<FilterIntentResult["material_status"]> | null;
  terms: string[];
  sort: { key: MatrixSortKey; direction: MatrixSortDirection } | null;
  conditions: SemanticCondition[];
}

export function QuestionPreparationOverviewPage() {
  const { taskId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
  const urlQuery = searchParams.get("q") ?? "";
  const smartSearch = useImeSafeQuery({ value: urlQuery, onCommit: commitQuery });
  const intentQuery = useAnalyticsFilterIntent();
  const [intentState, setIntentState] = useState<{ taskId: string; question: string; result: FilterIntentResult } | null>(null);
  const [pendingIntent, setPendingIntent] = useState<{ taskId: string; question: string } | null>(null);
  const intentVersionRef = useRef(0);
  const contextRef = useRef({ taskId, query: urlQuery.trim() });
  contextRef.current = { taskId, query: urlQuery.trim() };
  const [resolution, setResolution] = useState<"idle" | "local" | "llm">("idle");
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [headerSort, setHeaderSort] = useState<{ key: MatrixSortKey; direction: MatrixSortDirection } | null>(null);

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
  const query = urlQuery.trim();
  const currentIntent = intentState?.taskId === taskId && intentState?.question === query ? intentState : null;
  const activeIntent = currentIntent?.result.recognized ? currentIntent.result : null;
  const unsupportedIntent = currentIntent && !currentIntent.result.recognized;
  const waitingForIntent = pendingIntent?.taskId === taskId && pendingIntent?.question === query;
  const localPlan = useMemo(() => parseQuestionPreparationQuery(query, locale), [locale, query]);
  const semanticPlan = useMemo(
    () => activeIntent
      ? questionPreparationPlanFromIntent(activeIntent, locale)
      : waitingForIntent || unsupportedIntent
        ? emptyQuestionPreparationPlan()
        : localPlan,
    [activeIntent, locale, localPlan, unsupportedIntent, waitingForIntent],
  );
  const effectiveSort = headerSort ?? semanticPlan.sort ?? { key: "number", direction: "asc" as const };
  const rows = useMemo(() => {
    const textFiltered = allRows.filter((row) => matchesQuestionPreparationPlan(row, semanticPlan, locale));
    const typeFiltered = selectedTypes.size
      ? textFiltered.filter((row) => selectedTypes.has(row.problem.type || tx(locale, "未分类", "Uncategorized")))
      : textFiltered;
    return sortMatrixRows(typeFiltered, effectiveSort.key, effectiveSort.direction, locale);
  }, [allRows, effectiveSort.direction, effectiveSort.key, locale, selectedTypes, semanticPlan]);
  const metrics = useMemo(() => ({
    questions: new Set(allRisks.map((row) => row.problem.q_id)).size,
    lowConfidence: allRisks.filter((row) => row.issue.code === "low_confidence").length,
    conflicts: allRisks.filter((row) => ["source_conflict", "ai_source_conflict", "rubric_step_reference_conflict"].includes(row.issue.code)).length,
    anomalies: allRisks.filter((row) => ["parse_anomaly", "generation_failed", "invalid_test_case", "reference_solution_failed_case"].includes(row.issue.code)).length,
  }), [allRisks]);
  const recoveryInfo = intentQuery.isError
    ? classifyRecoverableError(intentQuery.error, {
      locale,
      phase: "analytics_filter_intent",
      returnTo: taskId ? `/tasks/${encodeURIComponent(taskId)}/questions` : "/history",
    })
    : null;

  if (taskQuery.isSuccess && taskQuery.data.status === "draft") {
    return <Navigate replace to={`/tasks/${taskId}/upload/problems`} />;
  }
  if (taskQuery.isSuccess && taskQuery.data.status === "extracting_problems") {
    return <Navigate replace to={`/tasks/${taskId}/problems/progress`} />;
  }

  function commitQuery(value: string) {
    const next = new URLSearchParams(searchParams);
    if (value.trim()) next.set("q", value);
    else next.delete("q");
    setSearchParams(next, { replace: true });
  }

  function clearIntent() {
    intentVersionRef.current += 1;
    setIntentState(null);
    setPendingIntent(null);
    setResolution("idle");
    intentQuery.reset();
  }

  function toggleSort(key: MatrixSortKey) {
    setHeaderSort((current) => current?.key === key
      ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
      : { key, direction: "asc" });
  }

  function removeSemanticCondition(condition: SemanticCondition) {
    const start = query.toLocaleLowerCase().indexOf(condition.source.toLocaleLowerCase());
    if (start < 0) return;
    const nextQuery = `${query.slice(0, start)} ${query.slice(start + condition.source.length)}`.replace(/\s+/g, " ").trim();
    clearIntent();
    smartSearch.commitValue(nextQuery);
  }

  function applySmartFilter(value: string) {
    if (intentQuery.isPending) return;
    const question = value.trim();
    clearIntent();
    smartSearch.commitValue(question);
    if (!question) return;
    const plan = parseQuestionPreparationQuery(question, locale);
    if (!questionPreparationQueryNeedsIntentFallback(plan, allRows, locale)) {
      setResolution("local");
      return;
    }
    if (!taskId) return;
    const version = ++intentVersionRef.current;
    setPendingIntent({ taskId, question });
    intentQuery.mutate({ taskId, question, surface: "question_preparation" }, {
      onSuccess: (result) => {
        if (intentVersionRef.current !== version || contextRef.current.taskId !== taskId || contextRef.current.query !== question) return;
        setPendingIntent(null);
        setIntentState({ taskId, question, result });
        setResolution("llm");
      },
      onError: () => {
        if (intentVersionRef.current === version) setPendingIntent(null);
      },
    });
  }

  function submitSmartFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    applySmartFilter(smartSearch.draftValue);
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

        <form onSubmit={submitSmartFilter} className="mt-4">
          <div className="flex items-center gap-2">
            <SmarTAIMascot variant={intentQuery.isPending ? "grading" : "thinking"} size="xs" />
            <label className="relative min-w-0 flex-1">
              <span className="sr-only">{tx(locale, "向 SmarTAI 描述题目资料筛选条件", "Ask SmarTAI to filter question materials")}</span>
              <input
                type="search"
                inputMode="search"
                value={smartSearch.draftValue}
                onBlur={smartSearch.handleBlur}
                onCompositionStart={smartSearch.handleCompositionStart}
                onCompositionEnd={smartSearch.handleCompositionEnd}
                onChange={(event) => { clearIntent(); smartSearch.handleChange(event); }}
                disabled={intentQuery.isPending}
                placeholder={tx(locale, "例如：按满分升序；缺少标答；低置信题优先", "For example: lowest max score first; missing reference answer; low-confidence questions")}
                className="h-12 w-full rounded-[10px] border bg-card pl-4 pr-36 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
              />
              {smartSearch.draftValue ? <button type="button" onClick={() => { clearIntent(); smartSearch.commitValue(""); }} aria-label={tx(locale, "清除智能筛选", "Clear smart filter")} className="absolute right-[7.25rem] top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"><X aria-hidden="true" className="h-4 w-4" /></button> : null}
              <button type="submit" disabled={intentQuery.isPending || !smartSearch.draftValue.trim()} className="absolute right-1 top-1/2 inline-flex h-9 -translate-y-1/2 items-center justify-center gap-1.5 rounded-[8px] bg-primary px-3 text-[11px] font-semibold text-primary-foreground disabled:opacity-50">{intentQuery.isPending ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : null}{intentQuery.isPending ? tx(locale, "理解中…", "Interpreting…") : tx(locale, "应用筛选", "Apply filter")}</button>
            </label>
          </div>
          <div className="mt-2 flex min-h-7 flex-wrap items-center gap-2">
            {semanticPlan.conditions.length ? semanticPlan.conditions.map((condition) => activeIntent ? (
              <span key={condition.id} className="inline-flex h-7 items-center rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary">{condition.label}</span>
            ) : (
              <button key={condition.id} type="button" onClick={() => removeSemanticCondition(condition)} title={tx(locale, "点击移除此条件", "Click to remove this condition")} className="inline-flex h-7 items-center gap-1 rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary hover:bg-blue-100">{condition.label}<X aria-hidden="true" className="h-3 w-3" /></button>
            )) : <span className="text-[11px] text-muted-foreground">{tx(locale, "本地规则优先；无法完整识别时，模型只解析这句指令，不会接收题目内容。", "Local rules run first. If needed, the model receives only this instruction—not question content.")}</span>}
            {resolution === "local" ? <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-600">{tx(locale, "本地规则已识别 · 未调用模型", "Matched locally · no model call")}</span> : null}
            {resolution === "llm" && currentIntent ? <><span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700">{unsupportedIntent ? tx(locale, "未能完整转换指令，未应用部分条件", "Could not interpret the full instruction; no partial filter applied") : tx(locale, "模型仅解析指令", "Model interpreted instruction only")}</span><span className="text-[11px] text-muted-foreground">{currentIntent.result.explanation}</span></> : null}
          </div>
          {recoveryInfo ? <RecoverableActionState info={recoveryInfo} locale={locale} compact className="mt-2" primaryAction={recoveryInfo.actionKind === "byok" ? undefined : { label: recoveryInfo.actionLabel, onClick: () => applySmartFilter(smartSearch.draftValue), busy: intentQuery.isPending }} /> : null}
        </form>

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
              sortKey={effectiveSort.key}
              sortDirection={effectiveSort.direction}
              availableTypes={availableTypes}
              selectedTypes={selectedTypes}
              onSort={toggleSort}
              onToggleType={toggleType}
              onClearTypes={() => setSelectedTypes(new Set())}
            />
          ) : <MatrixEmpty filtered={Boolean(query || selectedTypes.size)} locale={locale} />}
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
  const directionFor = (key: MatrixSortKey): TableSortDirection => sortKey === key ? sortDirection : null;
  return (
    <div className="max-h-[calc(100vh-520px)] min-h-[280px] overflow-auto overscroll-contain">
      <table className="w-full min-w-[1160px] border-collapse text-left text-[13px]">
        <thead className="sticky top-0 z-10 bg-muted/95 text-[12px] font-semibold text-muted-foreground backdrop-blur-sm">
          <tr className="border-b">
            <SortableTableHead className="w-[88px] px-5 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "题号", "No.")} direction={directionFor("number")} onSort={() => onSort("number")} ariaLabel={tx(locale, "按题号排序", "Sort by question number")} />
            <th className="relative w-[140px] px-3 py-3" aria-sort={directionFor("type") === "asc" ? "ascending" : directionFor("type") === "desc" ? "descending" : "none"}>
              <div className="flex items-center gap-1">
                <SortableHeaderButton label={tx(locale, "题型", "Type")} direction={directionFor("type")} onSort={() => onSort("type")} ariaLabel={tx(locale, "按题型排序", "Sort by type")} className="h-7 rounded-[5px] font-semibold" />
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
            <SortableTableHead className="w-[105px] px-3 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "满分", "Max Score")} direction={directionFor("max_score")} onSort={() => onSort("max_score")} ariaLabel={tx(locale, "按满分排序", "Sort by maximum score")} />
            <SortableTableHead className="w-[145px] px-3 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "题目", "Question")} direction={directionFor("stem")} onSort={() => onSort("stem")} ariaLabel={tx(locale, "按题目资料状态排序", "Sort by question material status")} />
            <SortableTableHead className="w-[145px] px-3 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "标答", "Reference Answer")} direction={directionFor("answer")} onSort={() => onSort("answer")} ariaLabel={tx(locale, "按标答资料状态排序", "Sort by reference-answer material status")} />
            <SortableTableHead className="w-[145px] px-3 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "评分标准", "Rubric")} direction={directionFor("rubric")} onSort={() => onSort("rubric")} ariaLabel={tx(locale, "按评分标准状态排序", "Sort by rubric status")} />
            <SortableTableHead className="w-[145px] px-3 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "测试样例", "Tests")} direction={directionFor("tests")} onSort={() => onSort("tests")} ariaLabel={tx(locale, "按测试样例状态排序", "Sort by test status")} />
            <SortableTableHead className="w-[145px] px-3 py-3" buttonClassName="h-7 rounded-[5px] font-semibold" label={tx(locale, "审核提示", "Attention")} direction={directionFor("attention")} onSort={() => onSort("attention")} ariaLabel={tx(locale, "按审核提示排序", "Sort by attention")} />
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

export function parseQuestionPreparationQuery(raw: string, locale: string): QuestionPreparationPlan {
  let remaining = raw.normalize("NFKC").trim();
  const conditions: SemanticCondition[] = [];
  const questionTokens: string[] = [];
  const types: string[] = [];
  let minMaxScore: number | null = null;
  let maxMaxScore: number | null = null;
  let preparationStatus: QuestionPreparationPlan["preparationStatus"] = null;
  let materialField: MaterialField | null = null;
  let materialStatus: QuestionPreparationPlan["materialStatus"] = null;
  let sort: QuestionPreparationPlan["sort"] = null;
  const consume = (regex: RegExp, id: string, label: (match: RegExpMatchArray) => string, apply: (match: RegExpMatchArray) => void) => {
    const match = remaining.match(regex);
    if (!match) return;
    apply(match);
    conditions.push({ id: `${id}-${conditions.length}`, label: label(match), source: match[0] });
    remaining = remaining.replace(match[0], " ");
  };

  for (const match of remaining.matchAll(/(?:\bq\s*|第\s*)(\d+(?:[._-]\d+)*)(?:\s*题)?(?![a-z0-9._-])/gi)) {
    questionTokens.push(match[1]);
    conditions.push({ id: `question-${conditions.length}`, label: tx(locale, `题号：Q${match[1]}`, `Question: Q${match[1]}`), source: match[0] });
    remaining = remaining.replace(match[0], " ");
  }

  const typeAliases: Array<[RegExp, string, string]> = [
    [/计算题|calculation/iu, "calculation", tx(locale, "题型：计算题", "Type: calculation")],
    [/编程题|程序题|代码题|programming|coding/iu, "programming", tx(locale, "题型：编程题", "Type: programming")],
    [/证明题|proof/iu, "proof", tx(locale, "题型：证明题", "Type: proof")],
    [/概念题|concept/iu, "concept", tx(locale, "题型：概念题", "Type: concept")],
  ];
  for (const [pattern, type, label] of typeAliases) {
    const match = remaining.match(pattern);
    if (!match) continue;
    types.push(type);
    conditions.push({ id: `type-${conditions.length}`, label, source: match[0] });
    remaining = remaining.replace(match[0], " ");
  }

  consume(/满分\s*(?:至少|不低于|大于等于|>=|≥)\s*(\d+(?:\.\d+)?)\s*(?:分|pts?)?/iu, "min-max", (match) => tx(locale, `满分 ≥ ${match[1]}`, `Maximum score ≥ ${match[1]}`), (match) => { minMaxScore = Number(match[1]); });
  consume(/满分\s*(?:低于|小于|不高于|<=|≤)\s*(\d+(?:\.\d+)?)\s*(?:分|pts?)?/iu, "max-max", (match) => tx(locale, `满分 ≤ ${match[1]}`, `Maximum score ≤ ${match[1]}`), (match) => { maxMaxScore = Number(match[1]); });

  consume(/低置信(?:度)?|low[\s-]*confidence/iu, "low-confidence", () => tx(locale, "低置信项", "Low-confidence item"), () => { preparationStatus = "low_confidence"; });
  if (!preparationStatus) consume(/来源冲突|资料冲突|source[\s-]*conflict/iu, "source-conflict", () => tx(locale, "来源冲突", "Source conflict"), () => { preparationStatus = "source_conflict"; });
  if (!preparationStatus) consume(/解析异常|识别异常|parse[\s-]*anomaly/iu, "parse-anomaly", () => tx(locale, "解析异常", "Parse anomaly"), () => { preparationStatus = "parse_anomaly"; });
  if (!preparationStatus) consume(/待审核|需核对|有风险|需要处理|attention/iu, "attention", () => tx(locale, "有审核提示", "Needs attention"), () => { preparationStatus = "attention"; });
  if (!preparationStatus) consume(/状态正常|无风险|已准备(?:好)?|ready/iu, "ready", () => tx(locale, "状态正常", "Ready"), () => { preparationStatus = "ready"; });

  const materialPatterns: Array<[RegExp, MaterialField, NonNullable<QuestionPreparationPlan["materialStatus"]>, string]> = [
    [/缺(?:少|失)?题目|题干缺失|missing\s+(?:question|stem)/iu, "stem", "missing", tx(locale, "缺少题目", "Missing question")],
    [/缺(?:少|失)?标答|没有标答|无标答|missing\s+(?:reference\s+)?answer/iu, "answer", "missing", tx(locale, "缺少标答", "Missing reference answer")],
    [/缺(?:少|失)?评分标准|没有评分标准|无评分标准|missing\s+(?:rubric|criterion)/iu, "rubric", "missing", tx(locale, "缺少评分标准", "Missing rubric")],
    [/缺(?:少|失)?测试(?:样例)?|没有测试(?:样例)?|无测试(?:样例)?|missing\s+tests?/iu, "tests", "missing", tx(locale, "缺少测试样例", "Missing tests")],
    [/已生成(?:标答|参考答案)|generated\s+(?:reference\s+)?answer/iu, "answer", "generated", tx(locale, "已生成标答", "Generated reference answer")],
    [/已生成(?:评分标准|rubric)|generated\s+rubric/iu, "rubric", "generated", tx(locale, "已生成评分标准", "Generated rubric")],
    [/已生成(?:测试(?:样例)?)|generated\s+tests?/iu, "tests", "generated", tx(locale, "已生成测试样例", "Generated tests")],
  ];
  for (const [pattern, field, status, label] of materialPatterns) {
    const match = remaining.match(pattern);
    if (!match) continue;
    materialField = field;
    materialStatus = status;
    conditions.push({ id: `material-${conditions.length}`, label, source: match[0] });
    remaining = remaining.replace(match[0], " ");
    break;
  }

  const sortPatterns: Array<[RegExp, MatrixSortKey, MatrixSortDirection, string]> = [
    [/(?:按)?满分\s*(?:从低到高|升序)|(?:sort\s+by\s+)?max(?:imum)?\s*score\s*(?:asc|low)/iu, "max_score", "asc", tx(locale, "满分从低到高", "Maximum score low to high")],
    [/(?:按)?满分\s*(?:从高到低|降序)|(?:sort\s+by\s+)?max(?:imum)?\s*score\s*(?:desc|high)/iu, "max_score", "desc", tx(locale, "满分从高到低", "Maximum score high to low")],
    [/(?:按)?题号\s*(?:从高到低|降序)|(?:sort\s+by\s+)?question\s*(?:desc|high)/iu, "number", "desc", tx(locale, "题号从高到低", "Question number high to low")],
    [/(?:按)?题号(?:排序|排列)?|(?:sort\s+by\s+)?question\s*(?:asc|low)?/iu, "number", "asc", tx(locale, "按题号", "Question order")],
    [/(?:按)?题型\s*(?:从高到低|降序)|(?:sort\s+by\s+)?type\s*(?:desc|z[\s-]*a)/iu, "type", "desc", tx(locale, "题型降序", "Type descending")],
    [/(?:按)?题型(?:排序|排列)?|(?:sort\s+by\s+)?type\s*(?:asc|a[\s-]*z)?/iu, "type", "asc", tx(locale, "按题型", "Type order")],
    [/低置信.*(?:优先|在前)|审核提示.*(?:优先|最多)|attention\s*(?:desc|first)|most\s+attention/iu, "attention", "desc", tx(locale, "审核提示优先", "Attention first")],
  ];
  for (const [pattern, key, direction, label] of sortPatterns) {
    const match = remaining.match(pattern);
    if (!match) continue;
    sort = { key, direction };
    conditions.push({ id: `sort-${conditions.length}`, label, source: match[0] });
    remaining = remaining.replace(match[0], " ");
    break;
  }

  remaining = remaining
    .replace(/[，,。；;：:、/]+/gu, " ")
    .replace(/(?:请|帮我|查看|显示|筛选|找出|只看|题目|资料|状态|并且|以及|同时|按)/giu, " ")
    .replace(/\s+/gu, " ")
    .trim();
  const terms = remaining.split(" ").map(normalizeText).filter(Boolean).slice(0, 4);
  for (const term of terms) conditions.push({ id: `term-${conditions.length}`, label: tx(locale, `匹配：${term}`, `Match: ${term}`), source: term });
  return { questionTokens, types, minMaxScore, maxMaxScore, preparationStatus, materialField, materialStatus, terms, sort, conditions };
}

function emptyQuestionPreparationPlan(): QuestionPreparationPlan {
  return { questionTokens: [], types: [], minMaxScore: null, maxMaxScore: null, preparationStatus: null, materialField: null, materialStatus: null, terms: [], sort: null, conditions: [] };
}

function questionPreparationPlanFromIntent(intent: FilterIntentResult, locale: string): QuestionPreparationPlan {
  const plan = emptyQuestionPreparationPlan();
  plan.questionTokens = intent.question_tokens;
  plan.types = intent.question_types ?? [];
  plan.minMaxScore = intent.min_max_score ?? null;
  plan.maxMaxScore = intent.max_max_score ?? null;
  plan.preparationStatus = intent.preparation_status ?? (intent.low_confidence ? "low_confidence" : null);
  plan.materialField = intent.material_field ?? null;
  plan.materialStatus = intent.material_status ?? null;
  plan.terms = intent.text_terms.map(normalizeText).filter(Boolean);
  plan.sort = questionPreparationSortFromIntent(intent);
  const add = (label: string) => plan.conditions.push({ id: `intent-${plan.conditions.length}`, label, source: "" });
  plan.questionTokens.forEach((token) => add(tx(locale, `题号：Q${token}`, `Question: Q${token}`)));
  plan.types.forEach((type) => add(tx(locale, `题型：${type}`, `Type: ${type}`)));
  if (plan.minMaxScore !== null) add(tx(locale, `满分 ≥ ${plan.minMaxScore}`, `Maximum score ≥ ${plan.minMaxScore}`));
  if (plan.maxMaxScore !== null) add(tx(locale, `满分 ≤ ${plan.maxMaxScore}`, `Maximum score ≤ ${plan.maxMaxScore}`));
  if (plan.preparationStatus) add(formatPreparationStatus(plan.preparationStatus, locale));
  if (plan.materialField && plan.materialStatus) add(formatMaterialCondition(plan.materialField, plan.materialStatus, locale));
  if (plan.sort) add(formatQuestionPreparationSort(plan.sort, locale));
  plan.terms.forEach((term) => add(tx(locale, `匹配：${term}`, `Match: ${term}`)));
  return plan;
}

function questionPreparationSortFromIntent(intent: FilterIntentResult): QuestionPreparationPlan["sort"] {
  switch (intent.sort) {
    case "question": return { key: "number", direction: "asc" };
    case "question_desc": return { key: "number", direction: "desc" };
    case "max_score_asc": return { key: "max_score", direction: "asc" };
    case "max_score_desc": return { key: "max_score", direction: "desc" };
    case "type_asc": return { key: "type", direction: "asc" };
    case "type_desc": return { key: "type", direction: "desc" };
    case "review_asc": return { key: "attention", direction: "asc" };
    case "review_desc": return { key: "attention", direction: "desc" };
    default: return null;
  }
}

function questionPreparationQueryNeedsIntentFallback(plan: QuestionPreparationPlan, rows: QuestionMatrixRow[], locale: string): boolean {
  if (!plan.conditions.length) return true;
  if (!plan.terms.length) return false;
  return !rows.some((row) => plan.terms.every((term) => questionPreparationRowText(row, locale).includes(term)));
}

function matchesQuestionPreparationPlan(row: QuestionMatrixRow, plan: QuestionPreparationPlan, locale: string): boolean {
  const maxScore = row.problem.max_score ?? 10;
  if (plan.questionTokens.length && !plan.questionTokens.some((token) => questionPreparationTokenMatches(row.problem, token))) return false;
  if (plan.types.length && !plan.types.some((type) => normalizeQuestionType(row.problem.type ?? "") === normalizeQuestionType(type))) return false;
  if (plan.minMaxScore !== null && maxScore < plan.minMaxScore) return false;
  if (plan.maxMaxScore !== null && maxScore > plan.maxMaxScore) return false;
  if (plan.preparationStatus && !matchesPreparationStatus(row, plan.preparationStatus)) return false;
  if (plan.materialField && plan.materialStatus && materialKind(row.problem, plan.materialField) !== plan.materialStatus) return false;
  const text = questionPreparationRowText(row, locale);
  return plan.terms.every((term) => text.includes(term));
}

function questionPreparationTokenMatches(problem: ProblemInfo, token: string): boolean {
  const normalized = normalizeText(token).replace(/^q/, "");
  return [problem.q_id, problem.number ?? ""]
    .map((value) => normalizeText(String(value)).replace(/^q/, ""))
    .some((value) => value === normalized);
}

function normalizeQuestionType(value: string): string {
  const normalized = normalizeText(value);
  const aliases: Record<string, string> = {
    "计算题": "calculation", "编程题": "programming", "程序题": "programming", "代码题": "programming",
    "证明题": "proof", "概念题": "concept",
  };
  return aliases[normalized] ?? normalized;
}

function matchesPreparationStatus(row: QuestionMatrixRow, status: NonNullable<QuestionPreparationPlan["preparationStatus"]>): boolean {
  if (status === "attention") return row.issues.length > 0;
  if (status === "ready") return row.issues.length === 0;
  if (status === "low_confidence") return row.issues.some((issue) => issue.code === "low_confidence");
  if (status === "source_conflict") return row.issues.some((issue) => issue.code === "source_conflict" || issue.code === "ai_source_conflict");
  return row.issues.some((issue) => issue.code === "parse_anomaly" || issue.code === "generation_failed");
}

function materialKind(problem: ProblemInfo, field: MaterialField): NonNullable<QuestionPreparationPlan["materialStatus"]> {
  const valueReady = field === "stem"
    ? Boolean(problem.stem?.trim())
    : field === "answer"
      ? Boolean(problem.reference_answer?.trim())
      : field === "rubric"
        ? Boolean(problem.criterion?.trim())
        : Boolean(problem.test_cases?.length);
  if (!valueReady) return "missing";
  if (field === "stem") return "recognized";
  const provenanceKey = field === "answer" ? "reference_answer" : field === "rubric" ? "criterion" : "test_cases";
  if (problem.material_provenance?.[provenanceKey]) return "recognized";
  if (problem.ai_completion_provenance?.[provenanceKey] || (field === "tests" && problem.test_cases?.every((item) => item.source === "llm_generated"))) return "generated";
  return "ready";
}

function questionPreparationRowText(row: QuestionMatrixRow, locale: string): string {
  const statuses = (["stem", "answer", "rubric", "tests"] as const).map((field) => getMaterialStatus(row.problem, field, locale).label);
  const sourceText = [
    row.problem.number,
    row.problem.q_id,
    row.problem.type,
    row.problem.max_score,
    row.problem.max_score_source,
    row.problem.max_score_review_status,
    row.problem.stem,
    row.problem.reference_answer,
    row.problem.criterion,
    ...statuses,
    ...(row.issues.flatMap((issue) => [issue.field, issue.code, issueCodeLabel(issue.code, locale), ...(issue.source_ids ?? [])])),
  ].filter(Boolean).join(" ");
  return normalizeText(`${sourceText} ${questionSearchAliases(sourceText)}`);
}

function formatPreparationStatus(status: NonNullable<QuestionPreparationPlan["preparationStatus"]>, locale: string): string {
  const labels: Record<NonNullable<QuestionPreparationPlan["preparationStatus"]>, [string, string]> = {
    attention: ["有审核提示", "Needs attention"], low_confidence: ["低置信项", "Low-confidence item"],
    source_conflict: ["来源冲突", "Source conflict"], parse_anomaly: ["解析异常", "Parse anomaly"], ready: ["状态正常", "Ready"],
  };
  return tx(locale, labels[status][0], labels[status][1]);
}

function formatMaterialCondition(field: MaterialField, status: NonNullable<QuestionPreparationPlan["materialStatus"]>, locale: string): string {
  const fieldLabel = tx(locale, { stem: "题目", answer: "标答", rubric: "评分标准", tests: "测试样例" }[field], { stem: "Question", answer: "Reference answer", rubric: "Rubric", tests: "Tests" }[field]);
  const statusLabel = tx(locale, { missing: "缺少", ready: "已准备", generated: "已生成", recognized: "已识别" }[status], { missing: "missing", ready: "ready", generated: "generated", recognized: "recognized" }[status]);
  return `${fieldLabel}：${statusLabel}`;
}

function formatQuestionPreparationSort(sort: NonNullable<QuestionPreparationPlan["sort"]>, locale: string): string {
  const labels: Record<MatrixSortKey, [string, string]> = {
    number: ["题号", "Question number"], type: ["题型", "Type"], max_score: ["满分", "Maximum score"],
    stem: ["题目资料状态", "Question material status"], answer: ["标答资料状态", "Reference-answer status"],
    rubric: ["评分标准状态", "Rubric status"], tests: ["测试样例状态", "Test status"], attention: ["审核提示", "Attention"],
  };
  return `${tx(locale, labels[sort.key][0], labels[sort.key][1])}${sort.direction === "asc" ? tx(locale, "升序", " ascending") : tx(locale, "降序", " descending")}`;
}

function normalizeText(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
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
      problem.max_score,
      problem.max_score_source,
      problem.max_score_review_status,
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
  const value = (row: QuestionMatrixRow): string | number => {
    if (key === "number") return row.problem.number || row.problem.q_id;
    if (key === "type") return row.problem.type || "";
    if (key === "max_score") return row.problem.max_score ?? 10;
    if (key === "stem" || key === "answer" || key === "rubric" || key === "tests") return materialSortRank(row.problem, key);
    return row.issues.length;
  };
  return [...rows].sort((left, right) => {
    const a = value(left);
    const b = value(right);
    const compared = typeof a === "number" && typeof b === "number"
      ? a - b
      : String(a).localeCompare(String(b), locale, { numeric: true });
    if (compared !== 0) return direction === "asc" ? compared : -compared;
    return (left.problem.number || left.problem.q_id).localeCompare(right.problem.number || right.problem.q_id, locale, { numeric: true, sensitivity: "base" });
  });
}

function materialSortRank(problem: ProblemInfo, field: MaterialField): number {
  const kind = materialKind(problem, field);
  return { missing: 0, ready: 1, generated: 2, recognized: 3 }[kind];
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
