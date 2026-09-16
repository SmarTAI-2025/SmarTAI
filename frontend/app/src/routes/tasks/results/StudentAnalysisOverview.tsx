import { ArrowRight, LoaderCircle, Search, X } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAnalyticsFilterIntent } from "@/api/hooks/analytics";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { SortableHeaderButton, SortableTableHead, type TableSortDirection } from "@/components/ui/SortableTableHead";
import {
  correctionScoreSource,
  effectiveCorrectionScore,
  formatConfidence,
  formatPercent,
  formatScore,
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "@/components/tasks/resultsModel";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { ResultsSummaryMetric as SummaryMetric } from "@/routes/tasks/results/ResultsSummaryMetric";
import type { Correction, FilterIntentResult } from "@/types";

type ScoreFilter = "all" | "under60" | "60to79" | "atleast80";
type PassFilter = "all" | "pass" | "fail" | "unscored";
type ConfidenceFilter = "all" | "low_items" | "avg_low";
type ReviewFilter = "all" | "pending" | "confirmed" | "none";
type ReviewState = Exclude<ReviewFilter, "all">;
type QuestionScoreSort = `question:${string}:asc` | `question:${string}:desc`;
type SortMode =
  | "id_asc"
  | "id_desc"
  | "name_asc"
  | "name_desc"
  | "total_asc"
  | "total_desc"
  | "score_asc"
  | "score_desc"
  | "status_asc"
  | "status_desc"
  | "confidence_asc"
  | "confidence_desc"
  | "review_asc"
  | "review_desc"
  | QuestionScoreSort;

interface StudentAnalysisRow {
  student: StudentSummary;
  correctionByQuestion: Map<string, Correction>;
  requiredReviewCount: number;
  confirmedReviewCount: number;
  hardFailureCount: number;
  disagreementCount: number;
  reviewState: ReviewState;
}

interface SemanticCondition {
  id: string;
  label: string;
  source: string;
}

interface SemanticStudentPlan {
  minPercent: number | null;
  maxPercent: number | null;
  pass: PassFilter | null;
  lowConfidence: boolean;
  reviewState: ReviewState | null;
  disagreement: boolean;
  sort: SortMode | null;
  terms: string[];
  conditions: SemanticCondition[];
}

type StudentSortColumn = "id" | "name" | "total" | "score" | "status" | "confidence" | "review";

const PAGE_SIZE = 5;

export function StudentAnalysisOverview({ locale, taskId, model }: { locale: Locale; taskId: string; model: ResultsModel }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const query = searchParams.get("q") ?? "";
  const smartSearch = useImeSafeQuery({ value: query, onCommit: (value) => updateParam("q", value, "") });
  const intentQuery = useAnalyticsFilterIntent();
  const [intentState, setIntentState] = useState<{ taskId: string; question: string; result: FilterIntentResult } | null>(null);
  const intentVersionRef = useRef(0);
  const contextRef = useRef({ taskId, query });
  contextRef.current = { taskId, query };
  const [resolution, setResolution] = useState<"idle" | "local" | "llm">("idle");
  const scoreFilter = normalizeScoreFilter(searchParams.get("score"));
  const passFilter = normalizePassFilter(searchParams.get("pass"));
  const confidenceFilter = normalizeConfidenceFilter(searchParams.get("confidence"));
  const reviewFilter = normalizeReviewFilter(searchParams.get("review"));
  const sortMode = normalizeSortMode(searchParams.get("sort"));
  const hasExplicitHeaderSort = searchParams.has("sort");
  const requestedPage = Math.max(1, Number(searchParams.get("page")) || 1);
  const returnQuery = searchParams.toString();

  const rows = useMemo(() => model.students.map(buildStudentRow), [model.students]);
  const localSemanticPlan = useMemo(() => parseSemanticStudentQuery(query, locale), [locale, query]);
  const currentIntent = intentState?.taskId === taskId && intentState.question === query ? intentState : null;
  const currentIntentSupported = currentIntent ? studentIntentSupported(currentIntent.result) : false;
  const activeIntent = currentIntent && currentIntentSupported ? currentIntent.result : null;
  const unsupportedIntent = currentIntent && !currentIntentSupported;
  const semanticPlan = useMemo(
    () => activeIntent ? intentToStudentPlan(activeIntent, locale) : unsupportedIntent ? parseSemanticStudentQuery("", locale) : localSemanticPlan,
    [activeIntent, localSemanticPlan, locale, unsupportedIntent],
  );
  const effectiveSort = hasExplicitHeaderSort ? sortMode : semanticPlan.sort ?? sortMode;
  const filteredRows = useMemo(() => rows
    .filter((row) => (
      matchesSemanticPlan(row, semanticPlan)
      && matchesScoreFilter(row.student.percent, scoreFilter)
      && matchesPassFilter(row.student.percent, passFilter)
      && matchesConfidenceFilter(row.student, confidenceFilter)
      && (reviewFilter === "all" || row.reviewState === reviewFilter)
    ))
    .sort((left, right) => compareRows(left, right, effectiveSort)), [confidenceFilter, effectiveSort, passFilter, reviewFilter, rows, scoreFilter, semanticPlan]);

  const pageCount = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
  const page = Math.min(requestedPage, pageCount);
  const visibleRows = filteredRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const validPercents = rows.map((row) => row.student.percent).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const passCount = validPercents.filter((value) => value >= 60).length;
  const mean = averageOrNull(validPercents);
  const median = medianOrNull(validPercents);
  const lowest = validPercents.length ? Math.min(...validPercents) : null;
  const highest = validPercents.length ? Math.max(...validPercents) : null;

  function updateParam(key: string, value: string, defaultValue = "all") {
    const next = new URLSearchParams(searchParams);
    if (!value || value === defaultValue) next.delete(key);
    else next.set(key, value);
    if (key !== "page") next.delete("page");
    setSearchParams(next, { replace: true });
  }

  function sortByHeader(column: StudentSortColumn) {
    intentVersionRef.current += 1;
    intentQuery.reset();
    const [ascending, descending] = studentSortPair(column);
    updateParam("sort", effectiveSort === ascending ? descending : ascending, "");
  }

  function sortByQuestion(questionId: string) {
    intentVersionRef.current += 1;
    intentQuery.reset();
    const ascending: QuestionScoreSort = `question:${questionId}:asc`;
    const descending: QuestionScoreSort = `question:${questionId}:desc`;
    updateParam("sort", effectiveSort === ascending ? descending : ascending, "");
  }

  const removeSemanticCondition = (condition: SemanticCondition) => {
    const start = query.toLocaleLowerCase().indexOf(condition.source.toLocaleLowerCase());
    if (start < 0) return;
    const nextQuery = `${query.slice(0, start)} ${query.slice(start + condition.source.length)}`.replace(/\s+/g, " ").trim();
    intentVersionRef.current += 1;
    updateParam("q", nextQuery, "");
    setIntentState(null);
    setResolution("idle");
    intentQuery.reset();
  };

  const applySmartFilter = (question: string) => {
    if (intentQuery.isPending) return;
    const normalized = question.trim();
    const intentVersion = ++intentVersionRef.current;
    smartSearch.commitValue(normalized);
    setIntentState(null);
    intentQuery.reset();
    if (!normalized) {
      setResolution("idle");
      return;
    }
    const plan = parseSemanticStudentQuery(normalized, locale);
    if (!studentQueryNeedsIntentFallback(plan, rows)) {
      setResolution("local");
      return;
    }
    intentQuery.mutate({ taskId, question: normalized, surface: "student_analysis" }, {
      onSuccess: (result) => {
        if (intentVersionRef.current !== intentVersion) return;
        if (contextRef.current.taskId !== taskId || contextRef.current.query !== normalized) return;
        setIntentState({ taskId, question: normalized, result });
        setResolution("llm");
      },
    });
  };

  const submitSmartFilter = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    applySmartFilter(smartSearch.draftValue);
  };

  const clearSmartFilter = () => {
    intentVersionRef.current += 1;
    setIntentState(null);
    setResolution("idle");
    intentQuery.reset();
    smartSearch.commitValue("");
  };

  const recoveryInfo = intentQuery.isError
    ? classifyRecoverableError(intentQuery.error, {
      locale,
      phase: "analytics_filter_intent",
      returnTo: `/tasks/${encodeURIComponent(taskId)}/results/students`,
    })
    : null;

  return (
    <section className="rounded-[10px] border bg-card">
      <div className="px-5 pt-5">
        <h2 className="text-[20px] font-bold tracking-[-0.01em] text-foreground">{tx(locale, "学生分析总览", "Student Performance Overview")}</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">{tx(locale, "查看全班总分、逐题得分与正式复核摘要；完整答案进入学生详情。", "Review class totals, per-question scores, and final review summaries. Full responses are available in each student's details.")}</p>

        <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-6">
          <SummaryMetric label={tx(locale, "学生数", "Students")} value={String(rows.length)} tone="primary" />
          <SummaryMetric label={tx(locale, "平均得分率", "Mean score")} value={formatPercent(mean)} tone="accent" />
          <SummaryMetric label={tx(locale, "中位得分率", "Median score")} value={formatPercent(median)} tone="secondary" />
          <SummaryMetric label={tx(locale, "最低 / 最高", "Lowest / highest")} value={`${formatPercent(lowest)} / ${formatPercent(highest)}`} tone="warning" />
          <SummaryMetric label={tx(locale, "及格率（≥60%）", "Pass rate (≥60%)")} value={formatPercent(validPercents.length ? (passCount / validPercents.length) * 100 : null)} tone="accent" />
          <SummaryMetric label={tx(locale, "含复核信号", "With review signals")} value={String(rows.filter((row) => row.requiredReviewCount > 0).length)} tone="danger" />
        </div>

        <form onSubmit={submitSmartFilter} className="mt-4">
          <label className="relative block">
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={smartSearch.draftValue}
              inputMode="search"
              onBlur={smartSearch.handleBlur}
              onCompositionStart={smartSearch.handleCompositionStart}
              onCompositionEnd={smartSearch.handleCompositionEnd}
              onChange={(event) => { intentVersionRef.current += 1; setIntentState(null); setResolution("idle"); intentQuery.reset(); smartSearch.handleChange(event); }}
              disabled={intentQuery.isPending}
              placeholder={tx(locale, "例如：90 分以下的学生；从高到低；低置信且待复核", "For example: students below 90; high to low; low confidence and pending review")}
              aria-label={tx(locale, "智能筛选学生", "Smart-filter students")}
              className="h-11 w-full rounded-[9px] border bg-background pl-10 pr-36 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
            />
            {smartSearch.draftValue ? <button type="button" onClick={clearSmartFilter} aria-label={tx(locale, "清除智能筛选", "Clear smart filter")} className="absolute right-[7.25rem] top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"><X aria-hidden="true" className="h-4 w-4" /></button> : null}
            <button type="submit" disabled={intentQuery.isPending || !smartSearch.draftValue.trim()} className="absolute right-1 top-1/2 inline-flex h-9 -translate-y-1/2 items-center justify-center gap-1.5 rounded-[8px] bg-primary px-3 text-[11px] font-semibold text-primary-foreground disabled:opacity-50">{intentQuery.isPending ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : null}{intentQuery.isPending ? tx(locale, "理解中…", "Interpreting…") : tx(locale, "应用筛选", "Apply filter")}</button>
          </label>
          <div className="mt-2 flex min-h-7 flex-wrap items-center gap-2">
            {semanticPlan.conditions.length ? semanticPlan.conditions.map((condition) => activeIntent ? (
              <span key={condition.id} className="inline-flex h-7 items-center rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary">{condition.label}</span>
            ) : (
              <button key={condition.id} type="button" onClick={() => removeSemanticCondition(condition)} title={tx(locale, "点击移除此条件", "Click to remove this condition")} className="inline-flex h-7 items-center gap-1 rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary hover:bg-blue-100">
                {condition.label}<X aria-hidden="true" className="h-3 w-3" />
              </button>
            )) : <span className="text-[11px] text-muted-foreground">{tx(locale, "本地预设优先；无法识别时，模型只解析这句指令，不会接收学生成绩。", "Local presets run first. If they cannot understand the query, the model sees only this instruction—not student scores.")}</span>}
            {resolution === "local" ? <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-600">{tx(locale, "本地规则已识别 · 未调用模型", "Matched locally · no model call")}</span> : null}
            {resolution === "llm" && currentIntent ? <><span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700">{unsupportedIntent ? tx(locale, "未能完整转换指令，未应用部分条件", "Could not interpret the full instruction; no partial filter applied") : tx(locale, "模型仅解析指令", "Model interpreted instruction only")}</span><span className="text-[11px] text-muted-foreground">{currentIntent.result.explanation}</span></> : null}
          </div>
          {recoveryInfo ? <RecoverableActionState info={recoveryInfo} locale={locale} compact className="mt-2" primaryAction={recoveryInfo.actionKind === "byok" ? undefined : { label: recoveryInfo.actionLabel, onClick: () => applySmartFilter(smartSearch.draftValue), busy: intentQuery.isPending }} secondaryAction={recoveryInfo.actionKind === "byok" ? { label: tx(locale, "关闭提示", "Dismiss"), onClick: () => intentQuery.reset() } : { label: tx(locale, "查看模型配置", "View model settings"), href: `/settings/byok?returnTo=${encodeURIComponent(`/tasks/${taskId}/results/students`)}` }} /> : null}
        </form>

        <div className="mt-3 grid grid-cols-2 gap-2 xl:grid-cols-4">
          <FilterSelect label={tx(locale, "得分率", "Score Percentage")} value={scoreFilter} onChange={(value) => updateParam("score", value)}>
            <option value="all">{tx(locale, "全部得分率", "All score percentages")}</option><option value="under60">{tx(locale, "低于 60%", "Below 60%")}</option><option value="60to79">60%–79%</option><option value="atleast80">{tx(locale, "80% 及以上", "80% and above")}</option>
          </FilterSelect>
          <FilterSelect label={tx(locale, "及格状态", "Pass status")} value={passFilter} onChange={(value) => updateParam("pass", value)}>
            <option value="all">{tx(locale, "全部状态", "All states")}</option><option value="pass">{tx(locale, "及格", "Passed")}</option><option value="fail">{tx(locale, "未及格", "Failed")}</option><option value="unscored">{tx(locale, "无可比总分", "No comparable total")}</option>
          </FilterSelect>
          <FilterSelect label={tx(locale, "置信度", "Confidence")} value={confidenceFilter} onChange={(value) => updateParam("confidence", value)}>
            <option value="all">{tx(locale, "全部置信度", "All confidence")}</option><option value="low_items">{tx(locale, "含低置信题次", "Has low-confidence items")}</option><option value="avg_low">{tx(locale, "平均低于 65%", "Mean below 65%")}</option>
          </FilterSelect>
          <FilterSelect label={tx(locale, "复核状态", "Review status")} value={reviewFilter} onChange={(value) => updateParam("review", value)}>
            <option value="all">{tx(locale, "全部复核状态", "All review states")}</option><option value="pending">{tx(locale, "有未人工处理信号", "Has unreviewed signals")}</option><option value="confirmed">{tx(locale, "信号已由教师处理", "Signals handled by teacher")}</option><option value="none">{tx(locale, "无复核信号", "No review signals")}</option>
          </FilterSelect>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 pb-3 text-[11px] text-muted-foreground">
          <span>{tx(locale, `匹配 ${filteredRows.length} / ${rows.length} 位学生`, `${filteredRows.length} / ${rows.length} students matched`)}</span>
          <span>{tx(locale, "逐题单元格显示得分 / 满分与得分率；点击可在学生详情聚焦该题。", "Per-question cells show score / maximum and rate; open one to focus that question in student detail.")}</span>
        </div>
      </div>

      {visibleRows.length ? (
        <>
          <StudentDesktopMatrix locale={locale} taskId={taskId} questions={model.questions} rows={visibleRows} returnQuery={returnQuery} sort={effectiveSort} onSort={sortByHeader} onSortQuestion={sortByQuestion} />
          <StudentMobileCards locale={locale} taskId={taskId} questions={model.questions} rows={visibleRows} returnQuery={returnQuery} />
        </>
      ) : <EmptyResult locale={locale} />}

      <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-t px-5 py-2.5 text-[11px] text-muted-foreground">
        <span>{filteredRows.length ? tx(locale, `显示 ${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, filteredRows.length)} / ${filteredRows.length}`, `Showing ${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, filteredRows.length)} of ${filteredRows.length}`) : tx(locale, "显示 0 位学生", "Showing 0 students")}</span>
        {pageCount > 1 ? <div className="flex items-center gap-2"><button type="button" disabled={page <= 1} onClick={() => updateParam("page", String(page - 1), "1")} className="h-8 rounded-[7px] border px-3 font-semibold text-foreground disabled:cursor-not-allowed disabled:opacity-40">{tx(locale, "上一页", "Previous")}</button><span>{page} / {pageCount}</span><button type="button" disabled={page >= pageCount} onClick={() => updateParam("page", String(page + 1), "1")} className="h-8 rounded-[7px] border px-3 font-semibold text-foreground disabled:cursor-not-allowed disabled:opacity-40">{tx(locale, "下一页", "Next")}</button></div> : null}
      </div>
    </section>
  );
}

function StudentDesktopMatrix({
  locale,
  taskId,
  questions,
  rows,
  returnQuery,
  sort,
  onSort,
  onSortQuestion,
}: {
  locale: Locale;
  taskId: string;
  questions: QuestionSummary[];
  rows: StudentAnalysisRow[];
  returnQuery: string;
  sort: SortMode;
  onSort: (column: StudentSortColumn) => void;
  onSortQuestion: (questionId: string) => void;
}) {
  const minWidth = Math.max(1120, 650 + questions.length * 92);
  const studentNameLabel = tx(locale, "姓名", "Name");
  const studentIdLabel = tx(locale, "学号", "Student ID");
  return (
    <div className="hidden border-t lg:block">
      <div className="max-w-full overflow-x-auto" tabIndex={0} aria-label={tx(locale, "学生逐题得分矩阵，可横向滚动", "Student per-question score matrix, horizontally scrollable")}>
        <table className="table-fixed text-left" style={{ minWidth }}>
          <thead className="bg-slate-50 text-[10px] font-medium text-muted-foreground"><tr>
            <th className="sticky left-0 z-10 w-[190px] bg-slate-50 px-4 py-3 font-medium">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <SortableHeaderButton label={studentNameLabel} direction={studentSortDirection(sort, "name")} onSort={() => onSort("name")} ariaLabel={studentSortAriaLabel(locale, studentNameLabel, studentSortDirection(sort, "name"))} />
                <SortableHeaderButton label={studentIdLabel} direction={studentSortDirection(sort, "id")} onSort={() => onSort("id")} ariaLabel={studentSortAriaLabel(locale, studentIdLabel, studentSortDirection(sort, "id"))} />
              </div>
            </th>
            <SortableTableHead label={tx(locale, "总分", "Total")} direction={studentSortDirection(sort, "total")} onSort={() => onSort("total")} ariaLabel={studentSortAriaLabel(locale, tx(locale, "总分", "Total"), studentSortDirection(sort, "total"))} className="w-[94px] px-3 py-3 font-medium" />
            <SortableTableHead label={tx(locale, "得分率", "Rate")} direction={studentSortDirection(sort, "score")} onSort={() => onSort("score")} ariaLabel={studentSortAriaLabel(locale, tx(locale, "得分率", "Rate"), studentSortDirection(sort, "score"))} className="w-[78px] px-3 py-3 font-medium" />
            <SortableTableHead label={tx(locale, "状态", "Status")} direction={studentSortDirection(sort, "status")} onSort={() => onSort("status")} ariaLabel={studentSortAriaLabel(locale, tx(locale, "状态", "Status"), studentSortDirection(sort, "status"))} className="w-[72px] px-3 py-3 font-medium" />
            <th className="w-[128px] px-3 py-3 font-medium">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <SortableHeaderButton label={tx(locale, "置信度", "Confidence")} direction={studentSortDirection(sort, "confidence")} onSort={() => onSort("confidence")} ariaLabel={studentSortAriaLabel(locale, tx(locale, "置信度", "Confidence"), studentSortDirection(sort, "confidence"))} />
                <SortableHeaderButton label={tx(locale, "复核", "Review")} direction={studentSortDirection(sort, "review")} onSort={() => onSort("review")} ariaLabel={studentSortAriaLabel(locale, tx(locale, "复核", "Review"), studentSortDirection(sort, "review"))} />
              </div>
            </th>
            {questions.map((question) => <SortableTableHead key={question.id} label={question.label} direction={questionScoreSortDirection(sort, question.id)} onSort={() => onSortQuestion(question.id)} ariaLabel={studentSortAriaLabel(locale, question.label, questionScoreSortDirection(sort, question.id))} className="w-[92px] px-2 py-3 text-center font-medium" buttonClassName="mx-auto font-semibold text-primary" />)}
            <th className="w-[68px] px-3 py-3 text-right font-medium">{tx(locale, "操作", "Action")}</th>
          </tr></thead>
          <tbody className="divide-y">{rows.map((row) => <StudentMatrixRow key={row.student.id} locale={locale} taskId={taskId} questions={questions} row={row} returnQuery={returnQuery} />)}</tbody>
        </table>
      </div>
    </div>
  );
}

function StudentMatrixRow({ locale, taskId, questions, row, returnQuery }: { locale: Locale; taskId: string; questions: QuestionSummary[]; row: StudentAnalysisRow; returnQuery: string }) {
  return (
    <tr className="hover:bg-slate-50/60">
      <td className="sticky left-0 z-[5] bg-card px-4 py-3 group-hover:bg-slate-50"><strong className="block truncate text-[12px] text-foreground">{row.student.name}</strong><span className="mt-0.5 block truncate text-[10px] text-muted-foreground">{row.student.id}</span></td>
      <td className="px-3 py-3 text-[11px] font-semibold text-foreground">{formatScore(row.student.totalScore)} / {formatScore(row.student.totalMax)}</td>
      <td className="px-3 py-3 text-[12px] font-bold text-primary">{formatPercent(row.student.percent)}</td>
      <td className="px-3 py-3"><PassBadge locale={locale} percent={row.student.percent} /></td>
      <td className="px-3 py-3"><span className="block text-[11px] font-semibold text-foreground">{formatConfidence(row.student.avgConfidence)}</span><ReviewBadge locale={locale} row={row} compact /></td>
      {questions.map((question) => <td key={question.id} className="px-2 py-2 text-center"><QuestionScoreLink locale={locale} taskId={taskId} studentId={row.student.id} question={question} correction={row.correctionByQuestion.get(question.id)} returnQuery={returnQuery} /></td>)}
      <td className="px-3 py-3 text-right"><Link to={studentDetailHref(taskId, row.student.id, null, returnQuery)} className="inline-flex items-center gap-1 text-[11px] font-semibold text-primary hover:underline">{tx(locale, "详情", "Details")}<ArrowRight aria-hidden="true" className="h-3 w-3" /></Link></td>
    </tr>
  );
}

function QuestionScoreLink({ locale, taskId, studentId, question, correction, returnQuery }: { locale: Locale; taskId: string; studentId: string; question: QuestionSummary; correction?: Correction; returnQuery: string }) {
  if (!correction) return <span className="text-[10px] text-muted-foreground">—</span>;
  const score = effectiveCorrectionScore(correction);
  const percent = score !== null && correction.max_score > 0
    ? (score / correction.max_score) * 100
    : null;
  return <Link to={studentDetailHref(taskId, studentId, question.id, returnQuery)} title={tx(locale, `在学生详情查看 ${question.label}`, `Open ${question.label} in student detail`)} className={cn("inline-flex min-w-[66px] flex-col rounded-[7px] px-2 py-1.5 hover:ring-1 hover:ring-primary/30", percent !== null && percent < 60 ? "bg-rose-50 text-rose-700" : "bg-muted/60 text-foreground")}><strong className="text-[10px]">{formatScore(score)} / {formatScore(correction.max_score)}</strong><span className="mt-0.5 text-[9px] opacity-75">{formatPercent(percent)}</span></Link>;
}

function StudentMobileCards({ locale, taskId, questions, rows, returnQuery }: { locale: Locale; taskId: string; questions: QuestionSummary[]; rows: StudentAnalysisRow[]; returnQuery: string }) {
  return <div className="grid gap-3 border-t p-4 lg:hidden">{rows.map((row) => (
    <article key={row.student.id} className="min-w-0 rounded-[9px] border p-3.5">
      <div className="flex items-start justify-between gap-3"><div className="min-w-0"><strong className="block truncate text-[14px] text-foreground">{row.student.name}</strong><span className="text-[11px] text-muted-foreground">{row.student.id}</span></div><Link to={studentDetailHref(taskId, row.student.id, null, returnQuery)} className="inline-flex shrink-0 items-center gap-1 text-[11px] font-semibold text-primary">{tx(locale, "详情", "Details")}<ArrowRight aria-hidden="true" className="h-3 w-3" /></Link></div>
      <div className="mt-3 grid grid-cols-3 gap-2"><SmallFact label={tx(locale, "总分", "Total")} value={`${formatScore(row.student.totalScore)} / ${formatScore(row.student.totalMax)}`} /><SmallFact label={tx(locale, "得分率", "Rate")} value={formatPercent(row.student.percent)} /><SmallFact label={tx(locale, "平均置信度", "Confidence")} value={formatConfidence(row.student.avgConfidence)} /></div>
      <div className="mt-3 flex flex-wrap items-center gap-2"><PassBadge locale={locale} percent={row.student.percent} /><ReviewBadge locale={locale} row={row} /></div>
      <div className="mt-3 w-full min-w-0 overflow-x-auto pb-1" tabIndex={0} aria-label={tx(locale, `${row.student.name} 的逐题得分，可横向滚动`, `${row.student.name}'s per-question scores, horizontally scrollable`)}>
        <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${Math.max(1, questions.length)}, minmax(74px, 1fr))`, minWidth: `${Math.max(1, questions.length) * 82}px` }}>{questions.map((question) => <div key={question.id} className="rounded-[7px] bg-muted/40 px-2 py-2"><span className="mb-1 block text-[10px] font-semibold text-muted-foreground">{question.label}</span><QuestionScoreLink locale={locale} taskId={taskId} studentId={row.student.id} question={question} correction={row.correctionByQuestion.get(question.id)} returnQuery={returnQuery} /></div>)}</div>
      </div>
    </article>
  ))}</div>;
}

function FilterSelect({ label, value, onChange, children }: { label: string; value: string; onChange: (value: string) => void; children: ReactNode }) {
  return <label><span className="sr-only">{label}</span><select value={value} onChange={(event) => onChange(event.target.value)} className="h-9 w-full rounded-[8px] border bg-background px-2.5 text-[11px] font-medium text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15">{children}</select></label>;
}

function SmallFact({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-[7px] bg-muted/60 px-2.5 py-2"><span className="block truncate text-[9px] text-muted-foreground">{label}</span><strong className="mt-0.5 block truncate text-[11px] text-foreground">{value}</strong></div>;
}

function PassBadge({ locale, percent }: { locale: Locale; percent: number | null }) {
  const pass = percent !== null && percent >= 60;
  const label = percent === null ? tx(locale, "无总分", "Unscored") : pass ? tx(locale, "及格", "Passed") : tx(locale, "未及格", "Failed");
  return <span className={cn("inline-flex rounded-full px-2 py-1 text-[9px] font-semibold", percent === null ? "bg-slate-100 text-slate-600" : pass ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700")}>{label}</span>;
}

function ReviewBadge({ locale, row, compact = false }: { locale: Locale; row: StudentAnalysisRow; compact?: boolean }) {
  const untouchedAiCount = Math.max(0, row.requiredReviewCount - row.confirmedReviewCount - row.hardFailureCount);
  const label = row.reviewState === "none"
    ? tx(locale, "无复核信号", "No review signal")
    : row.hardFailureCount
      ? tx(locale, `${row.hardFailureCount} 项无有效分数${untouchedAiCount ? ` · ${untouchedAiCount} 项 AI 分未人工处理` : ""}`, `${row.hardFailureCount} unscored${untouchedAiCount ? ` · ${untouchedAiCount} AI scores not reviewed` : ""}`)
      : row.reviewState === "confirmed"
        ? tx(locale, `${row.confirmedReviewCount}/${row.requiredReviewCount} 教师已处理`, `${row.confirmedReviewCount}/${row.requiredReviewCount} teacher handled`)
        : tx(locale, `${untouchedAiCount} 项 AI 分未人工处理`, `${untouchedAiCount} AI scores not reviewed`);
  return <span className={cn("inline-flex rounded-full font-semibold", compact ? "mt-1 px-1.5 py-0.5 text-[8px]" : "px-2 py-1 text-[9px]", row.reviewState === "none" && "bg-slate-100 text-slate-600", row.reviewState === "confirmed" && "bg-emerald-100 text-emerald-700", row.reviewState === "pending" && "bg-rose-100 text-rose-700")}>{label}</span>;
}

function EmptyResult({ locale }: { locale: Locale }) {
  return <div className="border-t px-5 py-12 text-center"><p className="text-[14px] font-bold text-foreground">{tx(locale, "没有匹配的学生", "No students matched")}</p><p className="mt-1 text-[12px] text-muted-foreground">{tx(locale, "移除一个条件，或清除智能筛选后重试。", "Remove a condition or clear the smart filter.")}</p></div>;
}

function buildStudentRow(student: StudentSummary): StudentAnalysisRow {
  const required = student.corrections.filter(correctionNeedsFormalReview);
  const confirmed = required.filter((correction) => correctionScoreSource(correction) !== "ai_untouched" && correctionScoreSource(correction) !== "hard_failure").length;
  const hardFailureCount = required.filter((correction) => correctionScoreSource(correction) === "hard_failure").length;
  return {
    student,
    correctionByQuestion: new Map(student.corrections.map((correction) => [correction.q_id, correction])),
    requiredReviewCount: required.length,
    confirmedReviewCount: confirmed,
    hardFailureCount,
    disagreementCount: student.corrections.filter(correctionHasDisagreement).length,
    reviewState: !required.length ? "none" : confirmed === required.length ? "confirmed" : "pending",
  };
}

export function parseSemanticStudentQuery(raw: string, locale: Locale): SemanticStudentPlan {
  let remaining = raw.normalize("NFKC").trim();
  const conditions: SemanticCondition[] = [];
  let minPercent: number | null = null;
  let maxPercent: number | null = null;
  let pass: PassFilter | null = null;
  let lowConfidence = false;
  let reviewState: ReviewState | null = null;
  let disagreement = false;
  let sort: SortMode | null = null;
  const consume = (regex: RegExp, id: string, label: (match: RegExpMatchArray) => string, apply: (match: RegExpMatchArray) => void) => {
    const match = remaining.match(regex);
    if (!match) return;
    apply(match);
    conditions.push({ id: `${id}-${conditions.length}`, label: label(match), source: match[0] });
    remaining = remaining.replace(match[0], " ");
  };

  consume(/(?:(?:得分率|总分率)?\s*(?:低于|小于|<)\s*(\d{1,3})\s*%?|(\d{1,3})\s*(?:分|%)?\s*(?:以下|以内|之下|及以下))/i, "max", (match) => tx(locale, `得分率 < ${match[1] ?? match[2]}%`, `Score < ${match[1] ?? match[2]}%`), (match) => { maxPercent = clampNumber(Number(match[1] ?? match[2]), 0, 101); });
  consume(/(?:得分率|总分率)?\s*(?:至少|不低于|大于等于|>=|≥)\s*(\d{1,3})\s*%?/i, "min", (match) => tx(locale, `得分率 ≥ ${match[1]}%`, `Score ≥ ${match[1]}%`), (match) => { minPercent = clampNumber(Number(match[1]), 0, 100); });
  consume(/不及格|未及格|fail(?:ed)?/i, "fail", () => tx(locale, "未及格", "Failed"), () => { pass = "fail"; });
  if (!pass) consume(/(?:^|\s)及格(?:\s|$)|pass(?:ed)?/i, "pass", () => tx(locale, "及格", "Passed"), () => { pass = "pass"; });
  consume(/低置信(?:度)?|low[\s-]*confidence/i, "confidence", () => tx(locale, "含低置信题次", "Has low-confidence items"), () => { lowConfidence = true; });
  consume(/专家分歧|模型分歧|评分差异|disagreement|score[\s-]*spread/i, "disagreement", () => tx(locale, "含专家分歧", "Has model disagreement"), () => { disagreement = true; });
  consume(/待复核|待确认|未复核|未人工处理|pending[\s-]*review|not[\s-]*reviewed/i, "pending", () => tx(locale, "有未人工处理信号", "Has unreviewed signals"), () => { reviewState = "pending"; });
  if (!reviewState) consume(/已复核|已确认|教师已处理|reviewed|confirmed|teacher[\s-]*handled/i, "confirmed", () => tx(locale, "信号已由教师处理", "Signals handled by teacher"), () => { reviewState = "confirmed"; });
  if (!reviewState) consume(/无复核|无需复核|no[\s-]*review/i, "none", () => tx(locale, "无复核信号", "No review signals"), () => { reviewState = "none"; });
  consume(/(?:按)?姓名\s*(?:升序|从[小低]到[大高]|a[\s-]*z)(?:排列|排序)?|(?:sort\s+(?:by\s+)?)?name\s*(?:asc(?:ending)?|a[\s-]*z)/i, "sort-name", () => tx(locale, "姓名升序", "Name A–Z"), () => { sort = "name_asc"; });
  if (!sort) consume(/(?:按)?姓名\s*(?:降序|从[大高]到[小低]|z[\s-]*a)(?:排列|排序)?|(?:sort\s+(?:by\s+)?)?name\s*(?:desc(?:ending)?|z[\s-]*a)/i, "sort-name-desc", () => tx(locale, "姓名降序", "Name Z–A"), () => { sort = "name_desc"; });
  if (!sort) consume(/(?:按)?(?:学号|学生\s*id|id)\s*(?:升序|从[小低]到[大高]|a[\s-]*z)|(?:sort\s+(?:by\s+)?)?(?:student\s*)?id\s*(?:asc(?:ending)?|a[\s-]*z)/i, "sort-id", () => tx(locale, "学号升序", "Student ID ascending"), () => { sort = "id_asc"; });
  if (!sort) consume(/(?:按)?(?:学号|学生\s*id|id)\s*(?:降序|从[大高]到[小低]|z[\s-]*a)|(?:sort\s+(?:by\s+)?)?(?:student\s*)?id\s*(?:desc(?:ending)?|z[\s-]*a)/i, "sort-id-desc", () => tx(locale, "学号降序", "Student ID descending"), () => { sort = "id_desc"; });
  if (!sort) consume(/总分\s*(?:从低到高|升序)|total\s*(?:asc|low)/i, "sort-total", () => tx(locale, "总分从低到高", "Total low to high"), () => { sort = "total_asc"; });
  if (!sort) consume(/总分\s*(?:从高到低|降序)|total\s*(?:desc|high)/i, "sort-total-desc", () => tx(locale, "总分从高到低", "Total high to low"), () => { sort = "total_desc"; });
  if (!sort) consume(/低分优先|得分(?:率)?从低到高|(?:^|\s)从低到高(?:\s|$)|score\s*(?:asc|low)/i, "sort-low", () => tx(locale, "低分优先", "Low score first"), () => { sort = "score_asc"; });
  if (!sort) consume(/高分优先|得分(?:率)?从高到低|(?:^|\s)从高到低(?:\s|$)|score\s*(?:desc|high)/i, "sort-high", () => tx(locale, "高分优先", "High score first"), () => { sort = "score_desc"; });
  if (!sort) consume(/置信度\s*(?:从低到高|升序)|confidence\s*(?:asc|low)/i, "sort-confidence", () => tx(locale, "置信度从低到高", "Confidence low to high"), () => { sort = "confidence_asc"; });
  if (!sort) consume(/置信度\s*(?:从高到低|降序)|confidence\s*(?:desc|high)/i, "sort-confidence-desc", () => tx(locale, "置信度从高到低", "Confidence high to low"), () => { sort = "confidence_desc"; });
  if (!sort) consume(/复核(?:信号|项)?\s*(?:从少到多|升序)|review\s*(?:asc|few)/i, "sort-review", () => tx(locale, "复核信号从少到多", "Fewest review signals first"), () => { sort = "review_asc"; });
  if (!sort) consume(/复核(?:信号|项)?\s*(?:最多|优先|从多到少|降序)|review\s*(?:desc|most)/i, "sort-review-desc", () => tx(locale, "复核信号最多优先", "Most review signals first"), () => { sort = "review_desc"; });
  if (!sort) consume(/状态\s*(?:升序|从低到高)|status\s*(?:asc|low)/i, "sort-status", () => tx(locale, "状态升序", "Status ascending"), () => { sort = "status_asc"; });
  if (!sort) consume(/状态\s*(?:降序|从高到低)|status\s*(?:desc|high)/i, "sort-status-desc", () => tx(locale, "状态降序", "Status descending"), () => { sort = "status_desc"; });

  remaining = remaining.replace(/学生|同学|哪些|所有|查看|显示|筛选|找出|请|的|了|一下/gi, " ");
  const terms = remaining.split(/[\s,，;；。.!！？?：:、/]+/).map(normalizeText).filter(Boolean);
  for (const term of terms) conditions.push({ id: `term-${conditions.length}`, label: tx(locale, `匹配：${term}`, `Match: ${term}`), source: term });
  return { minPercent, maxPercent, pass, lowConfidence, reviewState, disagreement, sort, terms, conditions };
}

function matchesSemanticPlan(row: StudentAnalysisRow, plan: SemanticStudentPlan): boolean {
  const percent = row.student.percent;
  if (plan.minPercent !== null && (percent === null || percent < plan.minPercent)) return false;
  if (plan.maxPercent !== null && (percent === null || percent >= plan.maxPercent)) return false;
  if (plan.pass && !matchesPassFilter(percent, plan.pass)) return false;
  if (plan.lowConfidence && row.student.lowConfidenceCount === 0) return false;
  if (plan.reviewState && row.reviewState !== plan.reviewState) return false;
  if (plan.disagreement && row.disagreementCount === 0) return false;
  const haystack = normalizeText(`${row.student.id} ${row.student.name}`);
  return plan.terms.every((term) => haystack.includes(term));
}

function intentToStudentPlan(intent: FilterIntentResult, locale: Locale): SemanticStudentPlan {
  const conditions: SemanticCondition[] = [];
  const add = (id: string, label: string) => conditions.push({ id, label, source: "" });
  if (intent.min_score_percent !== null) add("intent-min", tx(locale, `得分率 ≥ ${intent.min_score_percent}%`, `Score ≥ ${intent.min_score_percent}%`));
  if (intent.max_score_percent !== null) add("intent-max", tx(locale, `得分率 < ${intent.max_score_percent}%`, `Score < ${intent.max_score_percent}%`));
  if (intent.pass_status) add("intent-pass", intent.pass_status === "pass" ? tx(locale, "及格", "Passed") : intent.pass_status === "fail" ? tx(locale, "未及格", "Failed") : tx(locale, "无可比总分", "Unscored"));
  if (intent.low_confidence) add("intent-confidence", tx(locale, "含低置信题次", "Has low-confidence items"));
  if (intent.review_status) add("intent-review", intent.review_status === "pending" ? tx(locale, "有未人工处理信号", "Has unreviewed signals") : intent.review_status === "confirmed" ? tx(locale, "信号已由教师处理", "Signals handled") : tx(locale, "无复核信号", "No review signals"));
  if (intent.disagreement) add("intent-disagreement", tx(locale, "含专家分歧", "Has model disagreement"));
  const intentSort = studentSortFromIntent(intent);
  if (intentSort) add("intent-sort", formatIntentSort(intentSort, locale));
  for (const term of intent.text_terms) add(`intent-term-${conditions.length}`, tx(locale, `匹配：${term}`, `Match: ${term}`));
  return {
    minPercent: intent.min_score_percent,
    maxPercent: intent.max_score_percent,
    pass: intent.pass_status,
    lowConfidence: intent.low_confidence,
    reviewState: intent.review_status,
    disagreement: intent.disagreement,
    sort: studentSortFromIntent(intent),
    terms: intent.text_terms.map(normalizeText).filter(Boolean),
    conditions,
  };
}

function studentQueryNeedsIntentFallback(plan: SemanticStudentPlan, rows: StudentAnalysisRow[]): boolean {
  if (!plan.conditions.length) return true;
  if (!plan.terms.length) return false;
  return !rows.some((row) => {
    const haystack = normalizeText(`${row.student.id} ${row.student.name}`);
    return plan.terms.every((term) => haystack.includes(term));
  });
}

export function studentIntentSupported(intent: FilterIntentResult): boolean {
  if (!intent.recognized || (intent.sort !== null && studentSortFromIntent(intent) === null)) return false;
  return !intent.annotated
    && intent.question_tokens.length === 0
    && !(intent.question_types?.length)
    && intent.max_average_confidence == null
    && !intent.missing_knowledge
    && intent.min_max_score == null
    && intent.max_max_score == null
    && intent.preparation_status == null
    && intent.material_field == null
    && intent.material_status == null
    && intent.submission_status == null;
}

function studentSortFromIntent(intent: FilterIntentResult): SortMode | null {
  switch (intent.sort) {
    case "id_asc": return "id_asc";
    case "id_desc": return "id_desc";
    case "name_asc": return "name_asc";
    case "name_desc": return "name_desc";
    case "score_asc": return "score_asc";
    case "score_desc": return "score_desc";
    case "confidence_asc": return "confidence_asc";
    case "confidence_desc": return "confidence_desc";
    case "review_asc": return "review_asc";
    case "review_desc": return "review_desc";
    default: return null;
  }
}

function formatIntentSort(sort: SortMode, locale: Locale): string {
  if (sort === "id_asc") return tx(locale, "学号升序", "Student ID ascending");
  if (sort === "id_desc") return tx(locale, "学号降序", "Student ID descending");
  if (sort === "name_asc") return tx(locale, "姓名升序", "Name A–Z");
  if (sort === "name_desc") return tx(locale, "姓名降序", "Name Z–A");
  if (sort === "total_asc") return tx(locale, "总分从低到高", "Total low to high");
  if (sort === "total_desc") return tx(locale, "总分从高到低", "Total high to low");
  if (sort === "score_asc") return tx(locale, "得分率从低到高", "Score low to high");
  if (sort === "score_desc") return tx(locale, "得分率从高到低", "Score high to low");
  if (sort === "confidence_asc") return tx(locale, "置信度从低到高", "Confidence low to high");
  if (sort === "confidence_desc") return tx(locale, "置信度从高到低", "Confidence high to low");
  if (sort === "review_asc") return tx(locale, "复核信号从少到多", "Fewest review signals first");
  if (sort === "review_desc") return tx(locale, "复核信号最多优先", "Most review signals first");
  if (sort === "status_asc") return tx(locale, "状态升序", "Status ascending");
  if (sort === "status_desc") return tx(locale, "状态降序", "Status descending");
  return tx(locale, "按题目得分排序", "Sort by question score");
}

function matchesScoreFilter(percent: number | null, filter: ScoreFilter): boolean {
  if (filter === "all") return true;
  if (percent === null) return false;
  if (filter === "under60") return percent < 60;
  if (filter === "60to79") return percent >= 60 && percent < 80;
  return percent >= 80;
}

function matchesPassFilter(percent: number | null, filter: PassFilter): boolean {
  if (filter === "all") return true;
  if (filter === "unscored") return percent === null;
  if (percent === null) return false;
  return filter === "pass" ? percent >= 60 : percent < 60;
}

function matchesConfidenceFilter(student: StudentSummary, filter: ConfidenceFilter): boolean {
  if (filter === "all") return true;
  if (filter === "low_items") return student.lowConfidenceCount > 0;
  const confidence = normalizeConfidence(student.avgConfidence);
  return confidence !== null && confidence < 0.65;
}

function compareRows(left: StudentAnalysisRow, right: StudentAnalysisRow, sort: SortMode): number {
  const questionSort = parseQuestionScoreSort(sort);
  if (questionSort) {
    const leftPercent = scorePercentForQuestion(left, questionSort.questionId);
    const rightPercent = scorePercentForQuestion(right, questionSort.questionId);
    const delta = nullable(leftPercent, Number.POSITIVE_INFINITY) - nullable(rightPercent, Number.POSITIVE_INFINITY);
    return (questionSort.direction === "asc" ? delta : -delta) || compareStudents(left.student, right.student);
  }
  if (sort === "id_asc") return left.student.id.localeCompare(right.student.id, undefined, { numeric: true, sensitivity: "base" });
  if (sort === "id_desc") return right.student.id.localeCompare(left.student.id, undefined, { numeric: true, sensitivity: "base" });
  if (sort === "name_desc") return compareStudents(right.student, left.student);
  if (sort === "total_asc") return nullable(left.student.totalScore, Number.POSITIVE_INFINITY) - nullable(right.student.totalScore, Number.POSITIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "total_desc") return nullable(right.student.totalScore, Number.NEGATIVE_INFINITY) - nullable(left.student.totalScore, Number.NEGATIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "score_asc") return nullable(left.student.percent, Number.POSITIVE_INFINITY) - nullable(right.student.percent, Number.POSITIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "score_desc") return nullable(right.student.percent, Number.NEGATIVE_INFINITY) - nullable(left.student.percent, Number.NEGATIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "status_asc" || sort === "status_desc") {
    const delta = statusRank(left.student.percent) - statusRank(right.student.percent);
    return (sort === "status_asc" ? delta : -delta) || compareStudents(left.student, right.student);
  }
  if (sort === "confidence_asc") return nullable(normalizeConfidence(left.student.avgConfidence), Number.POSITIVE_INFINITY) - nullable(normalizeConfidence(right.student.avgConfidence), Number.POSITIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "confidence_desc") return nullable(normalizeConfidence(right.student.avgConfidence), Number.NEGATIVE_INFINITY) - nullable(normalizeConfidence(left.student.avgConfidence), Number.NEGATIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "review_asc" || sort === "review_desc") {
    const delta = reviewSignalCount(left) - reviewSignalCount(right);
    return (sort === "review_asc" ? delta : -delta) || compareStudents(left.student, right.student);
  }
  return compareStudents(left.student, right.student);
}

function scorePercentForQuestion(row: StudentAnalysisRow, questionId: string): number | null {
  const correction = row.correctionByQuestion.get(questionId);
  const score = correction ? effectiveCorrectionScore(correction) : null;
  return score !== null && correction && correction.max_score > 0 ? (score / correction.max_score) * 100 : null;
}

function statusRank(percent: number | null): number {
  if (percent === null) return 0;
  return percent < 60 ? 1 : 2;
}

function reviewSignalCount(row: StudentAnalysisRow): number {
  return Math.max(0, row.requiredReviewCount - row.confirmedReviewCount) + row.hardFailureCount + row.disagreementCount;
}

function correctionNeedsFormalReview(correction: Correction): boolean {
  const confidence = normalizeConfidence(correction.confidence);
  return (confidence !== null && confidence < 0.65) || correction.requires_human_review || correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high") || correction.synthesis_method === "all_failed" || correction.synthesis_method === "quota_exhausted" || correctionHasDisagreement(correction);
}

function correctionHasDisagreement(correction: Correction): boolean {
  if (correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")) return true;
  const scores = correction.expert_results?.map((result) => Number(result.score)).filter(Number.isFinite) ?? [];
  return scores.length > 1 && correction.max_score > 0 && Math.max(...scores) - Math.min(...scores) > Math.max(1, correction.max_score * 0.25);
}

function studentDetailHref(taskId: string, studentId: string, questionId: string | null, returnQuery: string): string {
  const path = `/tasks/${encodeURIComponent(taskId)}/results/students/${encodeURIComponent(studentId)}`;
  const params = new URLSearchParams();
  if (questionId) params.set("question", questionId);
  if (returnQuery) params.set("return", returnQuery);
  const query = params.toString();
  return query ? `${path}?${query}${questionId ? `#question-${encodeURIComponent(questionId)}` : ""}` : path;
}

function normalizeScoreFilter(value: string | null): ScoreFilter { return value === "under60" || value === "60to79" || value === "atleast80" ? value : "all"; }
function normalizePassFilter(value: string | null): PassFilter { return value === "pass" || value === "fail" || value === "unscored" ? value : "all"; }
function normalizeConfidenceFilter(value: string | null): ConfidenceFilter { return value === "low_items" || value === "avg_low" ? value : "all"; }
function normalizeReviewFilter(value: string | null): ReviewFilter { return value === "pending" || value === "confirmed" || value === "none" ? value : "all"; }
function normalizeSortMode(value: string | null): SortMode {
  const questionSort = parseQuestionScoreSort(value);
  if (questionSort) return `${"question"}:${questionSort.questionId}:${questionSort.direction}`;
  switch (value) {
    case "id_asc":
    case "id_desc":
    case "name_asc":
    case "name_desc":
    case "total_asc":
    case "total_desc":
    case "score_asc":
    case "score_desc":
    case "status_asc":
    case "status_desc":
    case "confidence_asc":
    case "confidence_desc":
    case "review_asc":
    case "review_desc":
      return value;
    default:
      return "name_asc";
  }
}

function studentSortPair(column: StudentSortColumn): [SortMode, SortMode] {
  switch (column) {
    case "id": return ["id_asc", "id_desc"];
    case "name": return ["name_asc", "name_desc"];
    case "total": return ["total_asc", "total_desc"];
    case "score": return ["score_asc", "score_desc"];
    case "status": return ["status_asc", "status_desc"];
    case "confidence": return ["confidence_asc", "confidence_desc"];
    case "review": return ["review_asc", "review_desc"];
  }
}

function studentSortDirection(sort: SortMode, column: StudentSortColumn): TableSortDirection {
  const [ascending, descending] = studentSortPair(column);
  return sort === ascending ? "asc" : sort === descending ? "desc" : null;
}

function questionScoreSortDirection(sort: SortMode, questionId: string): TableSortDirection {
  const questionSort = parseQuestionScoreSort(sort);
  return questionSort?.questionId === questionId ? questionSort.direction : null;
}

function parseQuestionScoreSort(value: string | null): { questionId: string; direction: "asc" | "desc" } | null {
  const match = value?.match(/^question:(.+):(asc|desc)$/);
  return match ? { questionId: match[1], direction: match[2] as "asc" | "desc" } : null;
}

function studentSortAriaLabel(locale: Locale, label: string, direction: TableSortDirection): string {
  if (locale === "en-US") {
    return direction === "asc"
      ? `${label}, ascending. Activate to sort descending.`
      : direction === "desc"
        ? `${label}, descending. Activate to sort ascending.`
        : `${label}. Activate to sort ascending.`;
  }
  return direction === "asc"
    ? `${label}，当前升序。点击改为降序。`
    : direction === "desc"
      ? `${label}，当前降序。点击改为升序。`
      : `${label}。点击按升序排序。`;
}
function normalizeConfidence(value: number | null | undefined): number | null { if (typeof value !== "number" || !Number.isFinite(value)) return null; return value > 1 ? value / 100 : value; }
function averageOrNull(values: number[]): number | null { return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null; }
function medianOrNull(values: number[]): number | null { if (!values.length) return null; const sorted = [...values].sort((a, b) => a - b); const middle = Math.floor(sorted.length / 2); return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2; }
function compareStudents(left: StudentSummary, right: StudentSummary): number { return left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" }) || left.id.localeCompare(right.id, undefined, { numeric: true, sensitivity: "base" }); }
function nullable(value: number | null, fallback: number): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function clampNumber(value: number, min: number, max: number): number { return Math.max(min, Math.min(max, value)); }
function normalizeText(value: string): string { return value.normalize("NFKC").trim().toLocaleLowerCase(); }
function tx(locale: Locale, zh: string, en: string): string { return locale === "en-US" ? en : zh; }
