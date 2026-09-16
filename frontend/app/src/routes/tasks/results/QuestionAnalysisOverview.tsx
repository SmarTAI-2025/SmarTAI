import { ArrowRight, LoaderCircle, X } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAnalyticsFilterIntent } from "@/api/hooks/analytics";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { SortableTableHead, compareSortableValues, toggleColumnSort, type ColumnSort } from "@/components/ui/SortableTableHead";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import {
  clampPercent,
  correctionScoreSource,
  formatConfidence,
  formatPercent,
  formatScore,
  LOW_CONFIDENCE_THRESHOLD,
  type QuestionSummary,
  type ResultsModel,
} from "@/components/tasks/resultsModel";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { ResultsSummaryMetric as SummaryMetric } from "@/routes/tasks/results/ResultsSummaryMetric";
import type { Correction, FilterIntentResult, ProblemInfo } from "@/types";

type ReviewFilter = "all" | "pending" | "confirmed" | "none";
type ReviewState = Exclude<ReviewFilter, "all">;
type ScoreFilter = "all" | "under60" | "under70" | "atleast80";
type ConfidenceFilter = "all" | "low_items" | "avg_low";
type SortMode = "question" | "question_desc" | "score_asc" | "score_desc" | "confidence_asc" | "confidence_desc" | "review_asc" | "review_desc" | "max_score_asc" | "max_score_desc" | "type_asc" | "type_desc";

interface QuestionAnalysisRow {
  question: QuestionSummary;
  label: string;
  type: string;
  stem: string;
  knowledgePoints: string[];
  avgConfidence: number | null;
  lowConfidenceCount: number;
  requiredReviewCount: number;
  confirmedReviewCount: number;
  hardFailureCount: number;
  reviewState: ReviewState;
  riskSummary: string;
}

interface SemanticCondition {
  id: string;
  label: string;
  source: string;
}

interface SemanticQuestionPlan {
  qTokens: string[];
  types: string[];
  minPercent: number | null;
  maxPercent: number | null;
  lowConfidence: boolean;
  avgConfidenceBelow: number | null;
  reviewState: ReviewState | null;
  missingKnowledge: boolean;
  sort: SortMode | null;
  terms: string[];
  conditions: SemanticCondition[];
}

export function QuestionAnalysisOverview({
  locale,
  taskId,
  model,
}: {
  locale: Locale;
  taskId: string;
  model: ResultsModel;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const query = searchParams.get("q") ?? "";
  const smartSearch = useImeSafeQuery({ value: query, onCommit: (value) => updateParam("q", value, "") });
  const intentQuery = useAnalyticsFilterIntent();
  const intentGeneration = useRef(0);
  const [columnSort, setColumnSort] = useState<ColumnSort | null>(null);
  const [intentState, setIntentState] = useState<{ taskId: string; question: string; result: FilterIntentResult } | null>(null);
  const contextRef = useRef({ taskId, query });
  contextRef.current = { taskId, query };
  const [resolution, setResolution] = useState<"idle" | "local" | "llm">("idle");
  const typeFilter = searchParams.get("type") ?? "all";
  const scoreFilter = normalizeScoreFilter(searchParams.get("score"));
  const confidenceFilter = normalizeConfidenceFilter(searchParams.get("confidence"));
  const reviewFilter = normalizeReviewFilter(searchParams.get("review"));
  const sortMode = normalizeSortMode(searchParams.get("sort"));
  const returnParams = new URLSearchParams(searchParams);
  returnParams.delete("page");
  const returnQuery = returnParams.toString();

  const rows = useMemo(() => model.questions.map((question) => buildQuestionRow(question, locale)), [locale, model.questions]);
  const currentIntent = intentState?.taskId === taskId && intentState.question === query ? intentState : null;
  const activeIntent = currentIntent?.result.recognized && questionIntentSupported(currentIntent.result) ? currentIntent.result : null;
  const unsupportedIntent = currentIntent && !activeIntent;
  const semanticPlan = useMemo(() => activeIntent ? intentToQuestionPlan(activeIntent, locale)
    : parseSemanticQuestionQuery(unsupportedIntent || (query && !localQuestionIntent(model.questions, query, locale)) ? "" : query, locale), [activeIntent, locale, model.questions, query, unsupportedIntent]);
  const effectiveSort = semanticPlan.sort ?? sortMode;
  const types = useMemo(
    () => Array.from(new Set(rows.map((row) => row.type).filter((value) => value !== "—"))).sort((a, b) => a.localeCompare(b, locale === "en-US" ? "en" : "zh-Hans-CN")),
    [locale, rows],
  );
  const filteredRows = useMemo(() => {
    const matches = rows.filter((row) => (
      matchesSemanticPlan(row, semanticPlan)
      && (typeFilter === "all" || normalizeText(row.type) === normalizeText(typeFilter))
      && matchesScoreFilter(row, scoreFilter)
      && matchesConfidenceFilter(row, confidenceFilter)
      && (reviewFilter === "all" || row.reviewState === reviewFilter)
    ));
    return matches.sort((left, right) => columnSort ? compareQuestionColumn(left, right, columnSort) : compareRows(left, right, effectiveSort));
  }, [columnSort, confidenceFilter, reviewFilter, rows, scoreFilter, semanticPlan, effectiveSort, typeFilter]);

  const averageQuestionPercent = averageOrNull(rows.map((row) => row.question.avgPercent));
  const weakQuestionCount = rows.filter((row) => (row.question.avgPercent ?? 100) < 60).length;
  const reviewSignalCount = rows.filter((row) => row.requiredReviewCount > 0).length;

  function updateParam(key: string, value: string, defaultValue = "all") {
    const next = new URLSearchParams(searchParams);
    if (!value || value === defaultValue) next.delete(key);
    else next.set(key, value);
    next.delete("page");
    setSearchParams(next, { replace: true });
  }

  const removeSemanticCondition = (condition: SemanticCondition) => {
    const start = query.toLocaleLowerCase().indexOf(condition.source.toLocaleLowerCase());
    if (start < 0) return;
    const nextQuery = `${query.slice(0, start)} ${query.slice(start + condition.source.length)}`.replace(/\s+/g, " ").trim();
    updateParam("q", nextQuery, "");
  };

  function sortColumn(key: string) {
    intentGeneration.current += 1;
    setColumnSort((current) => toggleColumnSort(current, key));
  }

  function clearIntent() {
    intentGeneration.current += 1;
    setIntentState(null);
    setResolution("idle");
    intentQuery.reset();
  }

  function applySmartFilter(value: string) {
    if (intentQuery.isPending) return;
    const question = value.trim();
    smartSearch.commitValue(question);
    clearIntent();
    setColumnSort(null);
    const generation = intentGeneration.current;
    if (!question) return;
    const plan = parseSemanticQuestionQuery(question, locale);
    if (plan.conditions.length && (!plan.terms.length || rows.some((row) => plan.terms.every((term) => termMatchesRow(term, row))))) {
      setResolution("local");
      return;
    }
    intentQuery.mutate({ taskId, question, surface: "question_analysis" }, {
      onSuccess: (result) => {
        if (generation !== intentGeneration.current || contextRef.current.taskId !== taskId || contextRef.current.query !== question) return;
        setIntentState({ taskId, question, result }); setResolution("llm");
      },
    });
  }

  function submitSmartFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    applySmartFilter(smartSearch.draftValue);
  }

  const recoveryInfo = intentQuery.isError ? classifyRecoverableError(intentQuery.error, {
    locale, phase: "analytics_filter_intent", returnTo: `/tasks/${encodeURIComponent(taskId)}/results/questions`,
  }) : null;

  return (
    <section className="rounded-[10px] border bg-card">
      <div className="px-5 pt-5">
        <h2 className="text-[20px] font-bold tracking-[-0.01em] text-foreground">
          {tx(locale, "题目分析总览", "Question analysis overview")}
        </h2>
        <p className="mt-1 text-[13px] text-muted-foreground">
          {tx(locale, "按题查看正式结果统计；学生完整答案只在学生详情中展开。", "Review final-results statistics by question. Full student responses are available in each student's details.")}
        </p>

        <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-4">
          <SummaryMetric label={tx(locale, "题目数", "Questions")} value={String(rows.length)} tone="primary" />
          <SummaryMetric label={tx(locale, "题目平均得分率", "Mean question score")} value={formatPercent(averageQuestionPercent)} tone="accent" />
          <SummaryMetric label={tx(locale, "低于 60%", "Below 60%")} value={String(weakQuestionCount)} tone="warning" />
          <SummaryMetric label={tx(locale, "含复核信号", "With review signals")} value={String(reviewSignalCount)} tone="danger" />
        </div>

        <form onSubmit={submitSmartFilter} className="mt-4">
          <div className="flex items-center gap-3">
            <SmarTAIMascot variant={intentQuery.isPending ? "grading" : "thinking"} size="xs" />
            <label className="relative block min-w-0 flex-1">
              <input
                value={smartSearch.draftValue}
                inputMode="search"
                onBlur={smartSearch.handleBlur}
                onCompositionStart={smartSearch.handleCompositionStart}
                onCompositionEnd={smartSearch.handleCompositionEnd}
                onChange={(event) => { clearIntent(); smartSearch.handleChange(event); }}
                disabled={intentQuery.isPending}
                placeholder={tx(locale, "例如：找出得分较低的计算题，按得分率从低到高排列", "For example: show calculation questions with low scores, lowest first")}
                aria-label={tx(locale, "智能筛选题目", "Smart-filter questions")}
                className="h-11 w-full rounded-[9px] border bg-background pl-3 pr-36 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
              />
              {smartSearch.draftValue ? (
                <button type="button" onClick={() => { clearIntent(); smartSearch.commitValue(""); }} aria-label={tx(locale, "清除智能筛选", "Clear smart filter")} className="absolute right-[7.25rem] top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground">
                  <X aria-hidden="true" className="h-4 w-4" />
                </button>
              ) : null}
              <button type="submit" disabled={intentQuery.isPending || !smartSearch.draftValue.trim()} className="absolute right-1 top-1/2 inline-flex h-9 -translate-y-1/2 items-center justify-center gap-1.5 rounded-[8px] bg-primary px-3 text-[11px] font-semibold text-primary-foreground disabled:opacity-50">{intentQuery.isPending ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : null}{intentQuery.isPending ? tx(locale, "理解中…", "Interpreting…") : tx(locale, "应用筛选", "Apply filter")}</button>
            </label>
          </div>

          <div className="mt-2 flex min-h-7 flex-wrap items-center gap-2">
            {semanticPlan.conditions.length ? semanticPlan.conditions.map((condition) => activeIntent ? (
              <span key={condition.id} className="inline-flex h-7 items-center rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary">{condition.label}</span>
            ) : (
              <button
                key={condition.id}
                type="button"
                onClick={() => removeSemanticCondition(condition)}
                className="inline-flex h-7 items-center gap-1 rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary hover:bg-blue-100"
                title={tx(locale, "点击移除此条件", "Click to remove this condition")}
              >
                {condition.label}
                <X aria-hidden="true" className="h-3 w-3" />
              </button>
            )) : (
              <span className="text-[11px] text-muted-foreground">
                {tx(locale, "本地预设优先；无法完整识别时，由模型解析指令，再筛选当前真实结果。", "Local presets run first; otherwise a model interprets the instruction and filters the current results.")}
              </span>
            )}
            {resolution === "local" ? <span className="text-[11px] text-muted-foreground">{tx(locale, "本地规则已识别 · 未调用模型", "Matched locally · no model call")}</span> : null}
            {resolution === "llm" && currentIntent ? <span role="status" className="text-[11px] text-muted-foreground">{unsupportedIntent ? tx(locale, "未能完整转换指令，未应用部分条件。", "Could not interpret the full instruction; no partial filter applied. ") : tx(locale, "模型仅解析指令：", "Model interpreted instruction only: ")}{currentIntent.result.explanation}</span> : null}
          </div>
          {recoveryInfo ? <RecoverableActionState info={recoveryInfo} locale={locale} compact className="mt-2" primaryAction={recoveryInfo.actionKind === "byok" ? undefined : { label: recoveryInfo.actionLabel, onClick: () => applySmartFilter(smartSearch.draftValue), busy: intentQuery.isPending }} /> : null}
        </form>

        <div className="mt-3 grid grid-cols-2 gap-2 xl:grid-cols-5">
          <FilterSelect value={typeFilter} onChange={(value) => updateParam("type", value)} label={tx(locale, "题型", "Type")}>
            <option value="all">{tx(locale, "全部题型", "All types")}</option>
            {types.map((type) => <option key={type} value={type}>{type}</option>)}
          </FilterSelect>
          <FilterSelect value={scoreFilter} onChange={(value) => updateParam("score", value)} label={tx(locale, "得分率", "Score Percentage")}>
            <option value="all">{tx(locale, "全部得分率", "All score percentages")}</option>
            <option value="under60">{tx(locale, "低于 60%", "Below 60%")}</option>
            <option value="under70">{tx(locale, "低于 70%", "Below 70%")}</option>
            <option value="atleast80">{tx(locale, "80% 及以上", "80% and above")}</option>
          </FilterSelect>
          <FilterSelect value={confidenceFilter} onChange={(value) => updateParam("confidence", value)} label={tx(locale, "置信度", "Confidence")}>
            <option value="all">{tx(locale, "全部置信度", "All confidence")}</option>
            <option value="low_items">{tx(locale, "含低置信题次", "Has low-confidence items")}</option>
            <option value="avg_low">{tx(locale, "平均置信度低于 65%", "Mean confidence below 65%")}</option>
          </FilterSelect>
          <FilterSelect value={reviewFilter} onChange={(value) => updateParam("review", value)} label={tx(locale, "复核状态", "Review status")}>
            <option value="all">{tx(locale, "全部复核状态", "All review states")}</option>
            <option value="pending">{tx(locale, "有未人工处理信号", "Has unreviewed signals")}</option>
            <option value="confirmed">{tx(locale, "信号已由教师处理", "Signals handled by teacher")}</option>
            <option value="none">{tx(locale, "无复核信号", "No review signals")}</option>
          </FilterSelect>

        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 pb-3 text-[11px] text-muted-foreground">
          <span>{tx(locale, `匹配 ${filteredRows.length} / ${rows.length} 道题`, `${filteredRows.length} / ${rows.length} questions matched`)}</span>
          {rows.some((row) => row.knowledgePoints.length === 0) ? (
            <span>{tx(locale, "知识点只显示后端真实标注；未标注时不由前端猜测。", "Knowledge points are shown only when provided; the UI does not invent them.")}</span>
          ) : null}
        </div>
      </div>

      {filteredRows.length ? (
        <>
          <QuestionDesktopTable locale={locale} taskId={taskId} rows={filteredRows} returnQuery={returnQuery} columnSort={columnSort} onSort={sortColumn} />
          <QuestionMobileCards locale={locale} taskId={taskId} rows={filteredRows} returnQuery={returnQuery} />
        </>
      ) : (
        <div className="border-t px-5 py-12 text-center">
          <p className="text-[14px] font-bold text-foreground">{tx(locale, "没有匹配的题目", "No questions matched")}</p>
          <p className="mt-1 text-[12px] text-muted-foreground">{tx(locale, "移除一个条件，或清除筛选条件后重试。", "Remove a condition or clear filters.")}</p>
        </div>
      )}

      <div className="flex min-h-12 items-center border-t px-5 py-2.5 text-[11px] text-muted-foreground">
        <span>
          {filteredRows.length
            ? tx(locale, `显示全部 ${filteredRows.length} 道题`, `Showing all ${filteredRows.length} questions`)
            : tx(locale, "显示 0 道题", "Showing 0 questions")}
        </span>
      </div>
    </section>
  );
}

function QuestionDesktopTable({ locale, taskId, rows, returnQuery, columnSort, onSort }: { locale: Locale; taskId: string; rows: QuestionAnalysisRow[]; returnQuery: string; columnSort: ColumnSort | null; onSort: (key: string) => void }) {
  return (
    <div className="hidden border-t lg:block">
      <table className="w-full table-fixed text-left">
        <thead className="bg-slate-50 text-[11px] font-medium text-muted-foreground">
          <tr>
            <SortableTableHead className="w-[31%] px-4 py-3 font-medium" direction={columnSort?.key === "question" ? columnSort.direction : null} onSort={() => onSort("question")}>{tx(locale, "题目", "Question")}</SortableTableHead>
            <SortableTableHead className="w-[9%] px-3 py-3 font-medium" direction={columnSort?.key === "count" ? columnSort.direction : null} onSort={() => onSort("count")}>{tx(locale, "作答", "Responses")}</SortableTableHead>
            <SortableTableHead className="w-[14%] px-3 py-3 font-medium" direction={columnSort?.key === "mean" ? columnSort.direction : null} onSort={() => onSort("mean")}>{tx(locale, "平均分", "Mean score")}</SortableTableHead>
            <SortableTableHead className="w-[13%] px-3 py-3 font-medium" direction={columnSort?.key === "confidence" ? columnSort.direction : null} onSort={() => onSort("confidence")}>{tx(locale, "置信度", "Confidence")}</SortableTableHead>
            <SortableTableHead className="w-[13%] px-3 py-3 font-medium" direction={columnSort?.key === "review" ? columnSort.direction : null} onSort={() => onSort("review")}>{tx(locale, "复核", "Review")}</SortableTableHead>
            <SortableTableHead className="w-[14%] px-3 py-3 font-medium" direction={columnSort?.key === "risk" ? columnSort.direction : null} onSort={() => onSort("risk")}>{tx(locale, "易错 / 风险摘要", "Error / risk summary")}</SortableTableHead>
            <th className="w-[6%] px-3 py-3 text-right font-medium">{tx(locale, "操作", "Action")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((row) => (
            <tr key={row.question.id} className="align-middle hover:bg-slate-50/60">
              <td className="px-4 py-3">
                <div className="flex items-center gap-2">
                  <strong className="text-[13px] text-foreground">{row.label}</strong>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">{row.type}</span>
                </div>
                <p className="mt-1 truncate text-[11px] text-muted-foreground">{row.stem || "—"}</p>
                <p className="mt-0.5 truncate text-[10px] text-muted-foreground">{knowledgeLabel(locale, row.knowledgePoints)}</p>
              </td>
              <td className="px-3 py-3 text-[12px] font-semibold text-foreground">{row.question.count}</td>
              <td className="px-3 py-3">
                <strong className="block text-[12px] text-foreground">{formatScore(row.question.avgScore)} / {formatScore(row.question.maxScore)}</strong>
                <span className="mt-0.5 block text-[11px] font-semibold text-primary">{formatPercent(row.question.avgPercent)}</span>
              </td>
              <td className="px-3 py-3">
                <span className="block text-[12px] font-semibold text-foreground">{formatConfidence(row.avgConfidence)}</span>
                <span className="mt-0.5 block text-[10px] text-muted-foreground">{tx(locale, `${row.lowConfidenceCount} 个低置信`, `${row.lowConfidenceCount} low-confidence`)}</span>
              </td>
              <td className="px-3 py-3"><ReviewBadge locale={locale} row={row} /></td>
              <td className="px-3 py-3 text-[11px] leading-4 text-muted-foreground">{row.riskSummary}</td>
              <td className="px-3 py-3 text-right">
                <Link to={questionDetailHref(taskId, row.question.id, returnQuery)} className="inline-flex items-center gap-1 text-[11px] font-semibold text-primary hover:underline">
                  {tx(locale, "详情", "Details")}<ArrowRight aria-hidden="true" className="h-3 w-3" />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QuestionMobileCards({ locale, taskId, rows, returnQuery }: { locale: Locale; taskId: string; rows: QuestionAnalysisRow[]; returnQuery: string }) {
  return (
    <div className="grid gap-3 border-t p-4 lg:hidden">
      {rows.map((row) => (
        <article key={row.question.id} className="rounded-[9px] border p-3.5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <strong className="text-[14px] text-foreground">{row.label}</strong>
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">{row.type}</span>
              </div>
              <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-muted-foreground">{row.stem || "—"}</p>
            </div>
            <Link to={questionDetailHref(taskId, row.question.id, returnQuery)} className="inline-flex shrink-0 items-center gap-1 text-[11px] font-semibold text-primary">
              {tx(locale, "详情", "Details")}<ArrowRight aria-hidden="true" className="h-3 w-3" />
            </Link>
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2">
            <SmallFact label={tx(locale, "作答", "Responses")} value={String(row.question.count)} />
            <SmallFact label={tx(locale, "平均得分率", "Mean score")} value={formatPercent(row.question.avgPercent)} />
            <SmallFact label={tx(locale, "平均置信度", "Confidence")} value={formatConfidence(row.avgConfidence)} />
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <ReviewBadge locale={locale} row={row} />
            <span className="text-[10px] text-muted-foreground">{knowledgeLabel(locale, row.knowledgePoints)}</span>
          </div>
          <p className="mt-2 rounded-[7px] bg-muted/60 px-2.5 py-2 text-[11px] leading-4 text-muted-foreground">{row.riskSummary}</p>
        </article>
      ))}
    </div>
  );
}

function FilterSelect({ label, value, onChange, children }: { label: string; value: string; onChange: (value: string) => void; children: ReactNode }) {
  return (
    <label className="min-w-0">
      <span className="sr-only">{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)} className="h-10 w-full rounded-[8px] border bg-background px-3 text-[12px] font-medium text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15">
        {children}
      </select>
    </label>
  );
}

function ReviewBadge({ locale, row }: { locale: Locale; row: QuestionAnalysisRow }) {
  const untouchedAiCount = Math.max(0, row.requiredReviewCount - row.confirmedReviewCount - row.hardFailureCount);
  const label = row.reviewState === "none"
    ? tx(locale, "无复核信号", "No review signal")
    : row.hardFailureCount
      ? tx(locale, `${row.hardFailureCount} 项无有效分数${untouchedAiCount ? ` · ${untouchedAiCount} 项 AI 分未人工处理` : ""}`, `${row.hardFailureCount} unscored${untouchedAiCount ? ` · ${untouchedAiCount} AI scores not reviewed` : ""}`)
      : row.reviewState === "confirmed"
        ? tx(locale, `${row.confirmedReviewCount}/${row.requiredReviewCount} 教师已处理`, `${row.confirmedReviewCount}/${row.requiredReviewCount} teacher handled`)
        : tx(locale, `${untouchedAiCount} 项 AI 分未人工处理`, `${untouchedAiCount} AI scores not reviewed`);
  return (
    <span className={cn(
      "inline-flex rounded-full px-2.5 py-1 text-[10px] font-semibold",
      row.reviewState === "confirmed" && "bg-emerald-100 text-emerald-700",
      row.reviewState === "pending" && "bg-rose-100 text-rose-700",
      row.reviewState === "none" && "bg-slate-100 text-slate-600",
    )}>{label}</span>
  );
}

function SmallFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[7px] bg-muted/60 px-2.5 py-2">
      <span className="block text-[10px] text-muted-foreground">{label}</span>
      <strong className="mt-0.5 block text-[12px] text-foreground">{value}</strong>
    </div>
  );
}

function buildQuestionRow(question: QuestionSummary, locale: Locale): QuestionAnalysisRow {
  const confidenceValues = question.entries
    .map((entry) => normalizeConfidence(entry.correction.confidence))
    .filter((value): value is number => value !== null);
  const requiredEntries = question.entries.filter((entry) => correctionNeedsFormalReview(entry.correction));
  const confirmedReviewCount = requiredEntries.filter((entry) => {
    const source = correctionScoreSource(entry.correction);
    return source === "teacher_confirmed_same" || source === "teacher_changed";
  }).length;
  const hardFailureCount = requiredEntries.filter((entry) => correctionScoreSource(entry.correction) === "hard_failure").length;
  const reviewState: ReviewState = !requiredEntries.length
    ? "none"
    : confirmedReviewCount === requiredEntries.length
      ? "confirmed"
      : "pending";
  const lowConfidenceCount = question.entries.filter((entry) => {
    const confidence = normalizeConfidence(entry.correction.confidence);
    return confidence !== null && confidence < LOW_CONFIDENCE_THRESHOLD;
  }).length;

  return {
    question,
    label: question.label,
    type: String(question.type || question.problem?.type || "—"),
    stem: String(question.stem || question.problem?.stem || ""),
    knowledgePoints: getKnowledgePoints(question.problem),
    avgConfidence: averageOrNull(confidenceValues),
    lowConfidenceCount,
    requiredReviewCount: requiredEntries.length,
    confirmedReviewCount,
    hardFailureCount,
    reviewState,
    riskSummary: buildRiskSummary(question, lowConfidenceCount, locale),
  };
}

function buildRiskSummary(question: QuestionSummary, lowConfidenceCount: number, locale: Locale): string {
  const parts: string[] = [];
  if (question.avgPercent !== null && question.avgPercent < 60) {
    parts.push(tx(locale, "平均得分率低于 60%", "mean score below 60%"));
  }
  if (lowConfidenceCount) {
    parts.push(tx(locale, `${lowConfidenceCount} 个低置信题次`, `${lowConfidenceCount} low-confidence items`));
  }
  const disagreementCount = question.entries.filter((entry) => (
    entry.correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")
    || hasExpertScoreSpread(entry.correction)
  )).length;
  if (disagreementCount) {
    parts.push(tx(locale, `${disagreementCount} 个专家分歧信号`, `${disagreementCount} expert-disagreement signals`));
  }
  return parts.length
    ? parts.join(tx(locale, "；", "; "))
    : tx(locale, "暂无由现有结果支持的明显风险信号", "No notable risk signal supported by current results");
}

function correctionNeedsFormalReview(correction: Correction): boolean {
  const confidence = normalizeConfidence(correction.confidence);
  if (confidence !== null && confidence < LOW_CONFIDENCE_THRESHOLD) return true;
  if (correction.requires_human_review) return true;
  if (correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")) return true;
  if (correction.synthesis_method === "all_failed" || correction.synthesis_method === "quota_exhausted") return true;
  return hasExpertScoreSpread(correction);
}

function hasExpertScoreSpread(correction: Correction): boolean {
  const scores = correction.expert_results
    ?.map((result) => Number(result.score))
    .filter((value) => Number.isFinite(value)) ?? [];
  return scores.length > 1
    && correction.max_score > 0
    && Math.max(...scores) - Math.min(...scores) > Math.max(1, correction.max_score * 0.25);
}

function getKnowledgePoints(problem?: ProblemInfo): string[] {
  if (!problem) return [];
  const record = problem as unknown as Record<string, unknown>;
  const raw = record.knowledge_points ?? record.knowledgePoints ?? record.knowledge_point ?? record.topic;
  const values = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(/[,，;；]/) : [];
  return Array.from(new Set(values.map((value) => String(value).trim()).filter(Boolean))).slice(0, 6);
}

export function parseSemanticQuestionQuery(rawQuery: string, locale: Locale): SemanticQuestionPlan {
  const query = rawQuery.trim();
  const plan: SemanticQuestionPlan = {
    qTokens: [],
    types: [],
    minPercent: null,
    maxPercent: null,
    lowConfidence: false,
    avgConfidenceBelow: null,
    reviewState: null,
    missingKnowledge: false,
    sort: null,
    terms: [],
    conditions: [],
  };
  if (!query) return plan;

  const consumed: string[] = [];
  const addCondition = (label: string, source: string) => {
    if (!source || consumed.some((item) => normalizeText(item) === normalizeText(source))) return;
    consumed.push(source);
    plan.conditions.push({ id: `${plan.conditions.length}-${source}`, label, source });
  };

  for (const match of query.matchAll(/\bq\s*(\d+(?:[._-]\d+)*)(?![a-z0-9._-])/gi)) {
    plan.qTokens.push(match[1]);
    addCondition(tx(locale, `题号：Q${match[1]}`, `Question: Q${match[1]}`), match[0]);
  }
  for (const match of query.matchAll(/第\s*([0-9]+)\s*题/g)) {
    plan.qTokens.push(match[1]);
    addCondition(tx(locale, `题号：Q${match[1]}`, `Question: Q${match[1]}`), match[0]);
  }

  const typeAliases = ["计算题", "编程题", "证明题", "概念题", "选择题", "填空题", "问答题"];
  for (const type of typeAliases) {
    if (normalizeText(query).includes(normalizeText(type))) {
      plan.types.push(type);
      addCondition(tx(locale, `题型：${type}`, `Type: ${type}`), type);
    }
  }

  const scoreBelow = query.match(/(?:平均)?得分率?\s*(?:低于|小于|少于|<)\s*(\d{1,3})\s*%?/i);
  if (scoreBelow) {
    plan.maxPercent = Math.min(100, Number(scoreBelow[1]));
    addCondition(tx(locale, `平均得分率 < ${plan.maxPercent}%`, `Mean score < ${plan.maxPercent}%`), scoreBelow[0]);
  } else {
    const weakWord = query.match(/薄弱|低分|错得多|错误多|较难|难题/);
    if (weakWord) {
      plan.maxPercent = 60;
      addCondition(tx(locale, "平均得分率 < 60%（薄弱）", "Mean score < 60% (weak)") , weakWord[0]);
    }
  }
  const scoreAbove = query.match(/(?:平均)?得分率?\s*(?:高于|大于|不少于|至少|>=|≥)\s*(\d{1,3})\s*%?/i);
  if (scoreAbove) {
    plan.minPercent = Math.min(100, Number(scoreAbove[1]));
    addCondition(tx(locale, `平均得分率 ≥ ${plan.minPercent}%`, `Mean score ≥ ${plan.minPercent}%`), scoreAbove[0]);
  }

  const confidenceBelow = query.match(/平均置信度\s*(?:低于|小于|<)\s*(\d{1,3})\s*%?/i);
  if (confidenceBelow) {
    plan.avgConfidenceBelow = Math.min(100, Number(confidenceBelow[1])) / 100;
    addCondition(tx(locale, `平均置信度 < ${confidenceBelow[1]}%`, `Mean confidence < ${confidenceBelow[1]}%`), confidenceBelow[0]);
  }
  const lowConfidence = query.match(/低置信(?:度|题次)?/);
  if (lowConfidence) {
    plan.lowConfidence = true;
    addCondition(tx(locale, "含低置信题次", "Has low-confidence items"), lowConfidence[0]);
  }

  const reviewMatch = query.match(/(?:仍需|待|需要)复核|待确认|未人工处理/);
  const confirmedMatch = query.match(/已复核|复核完成|已确认|教师已处理/);
  const noReviewMatch = query.match(/无必审|无需复核|没有复核项|无复核信号/);
  if (reviewMatch) {
    plan.reviewState = "pending";
    addCondition(tx(locale, "复核：有未人工处理信号", "Review: unreviewed signals"), reviewMatch[0]);
  } else if (confirmedMatch) {
    plan.reviewState = "confirmed";
    addCondition(tx(locale, "复核：信号已由教师处理", "Review: teacher handled"), confirmedMatch[0]);
  } else if (noReviewMatch) {
    plan.reviewState = "none";
    addCondition(tx(locale, "复核：无复核信号", "Review: no signal"), noReviewMatch[0]);
  }

  const missingKnowledge = query.match(/知识点未标注|未标注知识点|缺知识点/);
  if (missingKnowledge) {
    plan.missingKnowledge = true;
    addCondition(tx(locale, "知识点：未标注", "Knowledge point: unlabeled"), missingKnowledge[0]);
  }

  const sortPatterns: Array<[RegExp, SortMode, string]> = [
    [/得分率?(?:从低到高|升序)|低分优先/i, "score_asc", tx(locale, "得分率从低到高", "Score low to high")],
    [/得分率?(?:从高到低|降序)|高分优先/i, "score_desc", tx(locale, "得分率从高到低", "Score high to low")],
    [/置信度(?:从低到高|升序)/i, "confidence_asc", tx(locale, "置信度从低到高", "Confidence low to high")],
    [/复核信号最多优先/i, "review_desc", tx(locale, "复核信号最多优先", "Most review signals first")],
    [/按题号(?:排序|排列)?/i, "question", tx(locale, "按题号", "Question order")],
  ];
  for (const [pattern, sort, label] of sortPatterns) {
    const match = query.match(pattern);
    if (match) { plan.sort = sort; addCondition(label, match[0]); break; }
  }

  let remaining = query;
  for (const source of consumed) remaining = remaining.replace(source, " ");
  remaining = remaining
    .replace(/[，,。；;、]+/g, " ")
    .replace(/(?:请|帮我|查找|找出|显示|筛选|题目|知识点|并且|以及|同时)/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (remaining) {
    plan.terms = remaining.split(" ").filter(Boolean).slice(0, 8);
    for (const term of plan.terms) addCondition(tx(locale, `关键词：${term}`, `Keyword: ${term}`), term);
  }
  return plan;
}

function matchesSemanticPlan(row: QuestionAnalysisRow, plan: SemanticQuestionPlan): boolean {
  if (plan.qTokens.length && !plan.qTokens.some((token) => questionTokenMatches(row, token))) return false;
  if (plan.types.length && !plan.types.some((type) => normalizeQuestionType(row.type) === normalizeQuestionType(type))) return false;
  if (plan.maxPercent !== null && (row.question.avgPercent === null || row.question.avgPercent >= plan.maxPercent)) return false;
  if (plan.minPercent !== null && (row.question.avgPercent === null || row.question.avgPercent < plan.minPercent)) return false;
  if (plan.lowConfidence && row.lowConfidenceCount === 0) return false;
  if (plan.avgConfidenceBelow !== null && (row.avgConfidence === null || row.avgConfidence >= plan.avgConfidenceBelow)) return false;
  if (plan.reviewState && row.reviewState !== plan.reviewState) return false;
  if (plan.missingKnowledge && row.knowledgePoints.length > 0) return false;
  return plan.terms.every((term) => termMatchesRow(term, row));
}

function normalizeQuestionType(value: string): string {
  const normalized = normalizeText(value);
  const aliases: Record<string, string> = {
    "计算题": "calculation", "编程题": "programming", "证明题": "proof", "概念题": "concept",
    "选择题": "choice", "填空题": "fill_blank", "问答题": "short_answer",
  };
  return aliases[normalized] ?? normalized;
}

function intentToQuestionPlan(intent: FilterIntentResult, locale: Locale): SemanticQuestionPlan {
  const plan = parseSemanticQuestionQuery("", locale);
  plan.qTokens = intent.question_tokens;
  plan.types = intent.question_types ?? [];
  plan.minPercent = intent.min_score_percent;
  plan.maxPercent = intent.max_score_percent;
  plan.lowConfidence = intent.low_confidence;
  plan.avgConfidenceBelow = intent.max_average_confidence ?? null;
  plan.reviewState = intent.review_status;
  plan.missingKnowledge = intent.missing_knowledge ?? false;
  plan.sort = intent.sort as SortMode | null;
  plan.terms = intent.text_terms;
  const add = (label: string) => plan.conditions.push({ id: `intent-${plan.conditions.length}`, label, source: "" });
  plan.qTokens.forEach((token) => add(tx(locale, `题号：${token}`, `Question: ${token}`)));
  plan.types.forEach((type) => add(tx(locale, `题型：${type}`, `Type: ${type}`)));
  if (plan.minPercent !== null) add(tx(locale, `平均得分率 ≥ ${plan.minPercent}%`, `Mean score ≥ ${plan.minPercent}%`));
  if (plan.maxPercent !== null) add(tx(locale, `平均得分率 < ${plan.maxPercent}%`, `Mean score < ${plan.maxPercent}%`));
  if (plan.lowConfidence) add(tx(locale, "含低置信题次", "Has low-confidence items"));
  if (plan.avgConfidenceBelow !== null) add(tx(locale, `平均置信度 < ${plan.avgConfidenceBelow * 100}%`, `Mean confidence < ${plan.avgConfidenceBelow * 100}%`));
  if (plan.reviewState) add(plan.reviewState === "pending" ? tx(locale, "有未人工处理信号", "Has unreviewed signals") : plan.reviewState === "confirmed" ? tx(locale, "信号已由教师处理", "Signals handled by teacher") : tx(locale, "无复核信号", "No review signals"));
  if (plan.missingKnowledge) add(tx(locale, "知识点未标注", "Unlabeled knowledge points"));
  const sortLabels: Record<SortMode, string> = {
    question: tx(locale, "按题号", "Question order"), score_asc: tx(locale, "得分率从低到高", "Score low to high"),
    score_desc: tx(locale, "得分率从高到低", "Score high to low"), confidence_asc: tx(locale, "置信度从低到高", "Confidence low to high"),
    review_desc: tx(locale, "复核信号最多优先", "Most review signals first"),
    review_asc: tx(locale, "复核信号最少优先", "Fewest review signals first"),
    confidence_desc: tx(locale, "置信度从高到低", "Confidence high to low"),
    question_desc: tx(locale, "题号降序", "Question descending"),
    max_score_asc: tx(locale, "满分从低到高", "Maximum score low to high"),
    max_score_desc: tx(locale, "满分从高到低", "Maximum score high to low"),
    type_asc: tx(locale, "题型升序", "Type ascending"),
    type_desc: tx(locale, "题型降序", "Type descending"),
  };
  if (plan.sort) add(sortLabels[plan.sort]);
  plan.terms.forEach((term) => add(tx(locale, `关键词：${term}`, `Keyword: ${term}`)));
  return plan;
}

function questionTokenMatches(row: QuestionAnalysisRow, token: string): boolean {
  const normalized = normalizeText(token).replace(/^q/, "");
  const candidates = [row.question.id, row.label, row.question.problem?.number ?? ""]
    .map((value) => normalizeText(String(value)).replace(/^q/, ""));
  return candidates.some((candidate) => candidate === normalized);
}

function termMatchesRow(term: string, row: QuestionAnalysisRow): boolean {
  const normalized = normalizeText(term).replace(/题$/, "");
  const haystack = normalizeText([row.label, row.question.id, row.type, row.stem, ...row.knowledgePoints].join(" "));
  if (haystack.includes(normalized)) return true;
  if (normalized.includes("积分")) return haystack.includes("积分") || haystack.includes("\\int") || haystack.includes("integral");
  if (normalized.includes("微分") || normalized.includes("导数")) return haystack.includes("微分") || haystack.includes("导数") || haystack.includes("derivative");
  if (normalized.includes("证明")) return haystack.includes("证明") || haystack.includes("proof");
  if (normalized.includes("编程") || normalized.includes("代码")) return haystack.includes("编程") || haystack.includes("代码") || haystack.includes("program");
  return false;
}

function matchesScoreFilter(row: QuestionAnalysisRow, filter: ScoreFilter): boolean {
  const percent = row.question.avgPercent;
  if (filter === "all") return true;
  if (percent === null) return false;
  if (filter === "under60") return percent < 60;
  if (filter === "under70") return percent < 70;
  return percent >= 80;
}

function matchesConfidenceFilter(row: QuestionAnalysisRow, filter: ConfidenceFilter): boolean {
  if (filter === "all") return true;
  if (filter === "low_items") return row.lowConfidenceCount > 0;
  return row.avgConfidence !== null && row.avgConfidence < LOW_CONFIDENCE_THRESHOLD;
}

function compareRows(left: QuestionAnalysisRow, right: QuestionAnalysisRow, sort: SortMode): number {
  const direction = sort.endsWith("_desc") ? "desc" : "asc";
  const value = (row: QuestionAnalysisRow): string | number | null => {
    if (sort.startsWith("score_")) return row.question.avgPercent;
    if (sort.startsWith("confidence_")) return row.avgConfidence;
    if (sort.startsWith("review_")) return row.requiredReviewCount;
    if (sort.startsWith("max_score_")) return row.question.maxScore;
    if (sort.startsWith("type_")) return row.type;
    return row.label;
  };
  return compareSortableValues(value(left), value(right), direction) || compareQuestionLabels(left.label, right.label);
}

function compareQuestionColumn(left: QuestionAnalysisRow, right: QuestionAnalysisRow, sort: ColumnSort): number {
  const value = (row: QuestionAnalysisRow) => ({ question: row.label, count: row.question.count, mean: row.question.avgScore, confidence: row.avgConfidence, review: row.requiredReviewCount, risk: row.riskSummary })[sort.key];
  return compareSortableValues(value(left), value(right), sort.direction) || compareQuestionLabels(left.label, right.label);
}

const QUESTION_SORTS = new Set<string>(["question", "question_desc", "score_asc", "score_desc", "confidence_asc", "confidence_desc", "review_asc", "review_desc", "max_score_asc", "max_score_desc", "type_asc", "type_desc"]);
export function questionIntentSupported(intent: FilterIntentResult): boolean {
  return !intent.pass_status && !intent.annotated && !intent.disagreement && (!intent.sort || QUESTION_SORTS.has(intent.sort));
}

/** Shared by all three question-navigation Ask surfaces, with local facts only. */
export function localQuestionIntent(questions: QuestionSummary[], query: string, locale: Locale): FilterIntentResult | null {
  const plan = parseSemanticQuestionQuery(query, locale);
  const rows = questions.map((question) => buildQuestionRow(question, locale));
  if (query.trim() && (!plan.conditions.length || (plan.terms.length && !rows.some((row) => plan.terms.every((term) => termMatchesRow(term, row)))))) return null;
  return {
    recognized: true, min_score_percent: plan.minPercent, max_score_percent: plan.maxPercent,
    pass_status: null, low_confidence: plan.lowConfidence, review_status: plan.reviewState,
    disagreement: false, annotated: false, sort: plan.sort, question_tokens: plan.qTokens,
    question_types: plan.types, max_average_confidence: plan.avgConfidenceBelow,
    missing_knowledge: plan.missingKnowledge, text_terms: plan.terms, explanation: "",
  };
}

export function selectResultQuestions(questions: QuestionSummary[], intent: FilterIntentResult | null, locale: Locale): QuestionSummary[] {
  if (!intent || !intent.recognized || !questionIntentSupported(intent)) return questions;
  const plan = intentToQuestionPlan(intent, locale);
  return questions.map((question) => buildQuestionRow(question, locale)).filter((row) => matchesSemanticPlan(row, plan))
    .sort((a, b) => compareRows(a, b, plan.sort ?? "question")).map((row) => row.question);
}

function questionDetailHref(taskId: string, questionId: string, returnQuery: string): string {
  const detail = `/tasks/${encodeURIComponent(taskId)}/results/questions/${encodeURIComponent(questionId)}`;
  return returnQuery ? `${detail}?return=${encodeURIComponent(returnQuery)}` : detail;
}

function normalizeScoreFilter(value: string | null): ScoreFilter {
  return value === "under60" || value === "under70" || value === "atleast80" ? value : "all";
}

function normalizeConfidenceFilter(value: string | null): ConfidenceFilter {
  return value === "low_items" || value === "avg_low" ? value : "all";
}

function normalizeReviewFilter(value: string | null): ReviewFilter {
  return value === "pending" || value === "confirmed" || value === "none" ? value : "all";
}

function normalizeSortMode(value: string | null): SortMode {
  return value && QUESTION_SORTS.has(value) ? value as SortMode : "question";
}

function normalizeConfidence(value: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value > 1 ? value / 100 : value;
}

function averageOrNull(values: Array<number | null>): number | null {
  const clean = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  return clean.length ? clean.reduce((sum, value) => sum + value, 0) / clean.length : null;
}

function compareQuestionLabels(left: string, right: string): number {
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
}

function nullableNumber(value: number | null, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function normalizeText(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}

function knowledgeLabel(locale: Locale, points: string[]): string {
  return points.length
    ? tx(locale, `知识点：${points.join("、")}`, `Knowledge: ${points.join(", ")}`)
    : tx(locale, "知识点未标注", "Knowledge point not labeled");
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}
