import { ArrowRight, Search, X } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
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
import type { Correction } from "@/types";

type ScoreFilter = "all" | "under60" | "60to79" | "atleast80";
type PassFilter = "all" | "pass" | "fail" | "unscored";
type ConfidenceFilter = "all" | "low_items" | "avg_low";
type ReviewFilter = "all" | "pending" | "confirmed" | "none";
type ReviewState = Exclude<ReviewFilter, "all">;
type SortMode = "student" | "score_asc" | "score_desc" | "confidence_asc" | "review_desc";

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
  sort: SortMode | null;
  terms: string[];
  conditions: SemanticCondition[];
}

const PAGE_SIZE = 5;

export function StudentAnalysisOverview({ locale, taskId, model }: { locale: Locale; taskId: string; model: ResultsModel }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const query = searchParams.get("q") ?? "";
  const smartSearch = useImeSafeQuery({ value: query, onCommit: (value) => updateParam("q", value, "") });
  const scoreFilter = normalizeScoreFilter(searchParams.get("score"));
  const passFilter = normalizePassFilter(searchParams.get("pass"));
  const confidenceFilter = normalizeConfidenceFilter(searchParams.get("confidence"));
  const reviewFilter = normalizeReviewFilter(searchParams.get("review"));
  const sortMode = normalizeSortMode(searchParams.get("sort"));
  const requestedPage = Math.max(1, Number(searchParams.get("page")) || 1);
  const returnQuery = searchParams.toString();

  const rows = useMemo(() => model.students.map(buildStudentRow), [model.students]);
  const semanticPlan = useMemo(() => parseSemanticStudentQuery(query, locale), [locale, query]);
  const effectiveSort = semanticPlan.sort ?? sortMode;
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

  const removeSemanticCondition = (condition: SemanticCondition) => {
    const start = query.toLocaleLowerCase().indexOf(condition.source.toLocaleLowerCase());
    if (start < 0) return;
    const nextQuery = `${query.slice(0, start)} ${query.slice(start + condition.source.length)}`.replace(/\s+/g, " ").trim();
    updateParam("q", nextQuery, "");
  };

  return (
    <section className="rounded-[10px] border bg-card">
      <div className="px-5 pt-5">
        <h2 className="text-[20px] font-bold tracking-[-0.01em] text-foreground">{tx(locale, "学生分析总览", "Student Performance Overview")}</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">{tx(locale, "查看全班总分、逐题得分与正式复核摘要；完整答案进入学生详情。", "Review class totals, per-question scores, and final review summaries. Full responses are available in each student's details.")}</p>

        <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-6">
          <SummaryMetric label={tx(locale, "学生数", "Students")} value={String(rows.length)} tone="primary" />
          <SummaryMetric label={tx(locale, "平均得分率", "Mean score")} value={formatPercent(mean)} tone="accent" />
          <SummaryMetric label={tx(locale, "中位得分率", "Median score")} value={formatPercent(median)} tone="primary" />
          <SummaryMetric label={tx(locale, "最低 / 最高", "Lowest / highest")} value={`${formatPercent(lowest)} / ${formatPercent(highest)}`} tone="warning" />
          <SummaryMetric label={tx(locale, "及格率（≥60%）", "Pass rate (≥60%)")} value={formatPercent(validPercents.length ? (passCount / validPercents.length) * 100 : null)} tone="accent" />
          <SummaryMetric label={tx(locale, "含复核信号", "With review signals")} value={String(rows.filter((row) => row.requiredReviewCount > 0).length)} tone="danger" />
        </div>

        <div className="mt-4">
          <label className="relative block">
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={smartSearch.draftValue}
              inputMode="search"
              onBlur={smartSearch.handleBlur}
              onCompositionStart={smartSearch.handleCompositionStart}
              onCompositionEnd={smartSearch.handleCompositionEnd}
              onChange={smartSearch.handleChange}
              placeholder={tx(locale, "SmarTAI 智能搜索：例如 PB2011 不及格 低置信 待复核 低分优先", "SmarTAI Smart Search: PB2011 failed low confidence pending review low score first")}
              aria-label={tx(locale, "SmarTAI 自然语言筛选学生", "SmarTAI natural-language student filter")}
              className="h-11 w-full rounded-[9px] border bg-background pl-10 pr-10 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
            />
            {smartSearch.draftValue ? <button type="button" onClick={() => smartSearch.commitValue("")} aria-label={tx(locale, "清除 SmarTAI 自然语言筛选", "Clear SmarTAI natural-language filter")} className="absolute right-2 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"><X aria-hidden="true" className="h-4 w-4" /></button> : null}
          </label>
          <div className="mt-2 flex min-h-7 flex-wrap items-center gap-2">
            {semanticPlan.conditions.length ? semanticPlan.conditions.map((condition) => (
              <button key={condition.id} type="button" onClick={() => removeSemanticCondition(condition)} title={tx(locale, "点击移除此条件", "Click to remove this condition")} className="inline-flex h-7 items-center gap-1 rounded-full bg-blue-50 px-2.5 text-[11px] font-semibold text-primary hover:bg-blue-100">
                {condition.label}<X aria-hidden="true" className="h-3 w-3" />
              </button>
            )) : <span className="text-[11px] text-muted-foreground">{tx(locale, "本地可解释筛选，不消耗模型额度；姓名与学号直接匹配。", "Explainable local filtering with no model call; names and IDs match directly.")}</span>}
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-2 xl:grid-cols-5">
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
          <FilterSelect label={tx(locale, "排序", "Sort")} value={sortMode} onChange={(value) => updateParam("sort", value, "student")}>
            <option value="student">{tx(locale, "按姓名 / 学号", "Name / ID")}</option><option value="score_asc">{tx(locale, "得分率从低到高", "Score low to high")}</option><option value="score_desc">{tx(locale, "得分率从高到低", "Score high to low")}</option><option value="confidence_asc">{tx(locale, "置信度从低到高", "Confidence low to high")}</option><option value="review_desc">{tx(locale, "复核信号最多优先", "Most review signals first")}</option>
          </FilterSelect>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 pb-3 text-[11px] text-muted-foreground">
          <span>{tx(locale, `匹配 ${filteredRows.length} / ${rows.length} 位学生`, `${filteredRows.length} / ${rows.length} students matched`)}</span>
          <span>{tx(locale, "逐题单元格显示得分 / 满分与得分率；点击可在学生详情聚焦该题。", "Per-question cells show score / maximum and rate; open one to focus that question in student detail.")}</span>
        </div>
      </div>

      {visibleRows.length ? (
        <>
          <StudentDesktopMatrix locale={locale} taskId={taskId} questions={model.questions} rows={visibleRows} returnQuery={returnQuery} />
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

function StudentDesktopMatrix({ locale, taskId, questions, rows, returnQuery }: { locale: Locale; taskId: string; questions: QuestionSummary[]; rows: StudentAnalysisRow[]; returnQuery: string }) {
  const minWidth = Math.max(1120, 650 + questions.length * 92);
  return (
    <div className="hidden border-t lg:block">
      <div className="max-w-full overflow-x-auto" tabIndex={0} aria-label={tx(locale, "学生逐题得分矩阵，可横向滚动", "Student per-question score matrix, horizontally scrollable")}>
        <table className="table-fixed text-left" style={{ minWidth }}>
          <thead className="bg-slate-50 text-[10px] font-medium text-muted-foreground"><tr>
            <th className="sticky left-0 z-10 w-[190px] bg-slate-50 px-4 py-3 font-medium">{tx(locale, "学生", "Student")}</th>
            <th className="w-[94px] px-3 py-3 font-medium">{tx(locale, "总分", "Total")}</th><th className="w-[78px] px-3 py-3 font-medium">{tx(locale, "得分率", "Rate")}</th><th className="w-[72px] px-3 py-3 font-medium">{tx(locale, "状态", "Status")}</th><th className="w-[128px] px-3 py-3 font-medium">{tx(locale, "置信 / 复核", "Confidence / review")}</th>
            {questions.map((question) => <th key={question.id} className="w-[92px] px-2 py-3 text-center font-medium"><Link to={`/tasks/${encodeURIComponent(taskId)}/results/questions/${encodeURIComponent(question.id)}`} className="font-semibold text-primary hover:underline">{question.label}</Link></th>)}
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

function SummaryMetric({ label, value, tone }: { label: string; value: string; tone: "primary" | "accent" | "warning" | "danger" }) {
  return <div className="rounded-[9px] border px-3 py-3"><strong className={cn("text-[18px] leading-6", tone === "primary" && "text-primary", tone === "accent" && "text-teal-500", tone === "warning" && "text-amber-500", tone === "danger" && "text-rose-500")}>{value}</strong><span className="mt-1 block text-[10px] font-medium text-muted-foreground">{label}</span></div>;
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
  return <div className="border-t px-5 py-12 text-center"><p className="text-[14px] font-bold text-foreground">{tx(locale, "没有匹配的学生", "No students matched")}</p><p className="mt-1 text-[12px] text-muted-foreground">{tx(locale, "移除一个条件，或清除 SmarTAI 智能筛选后重试。", "Remove a condition or clear the SmarTAI Smart Filter.")}</p></div>;
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

function parseSemanticStudentQuery(raw: string, locale: Locale): SemanticStudentPlan {
  let remaining = raw.normalize("NFKC").trim();
  const conditions: SemanticCondition[] = [];
  let minPercent: number | null = null;
  let maxPercent: number | null = null;
  let pass: PassFilter | null = null;
  let lowConfidence = false;
  let reviewState: ReviewState | null = null;
  let sort: SortMode | null = null;
  const consume = (regex: RegExp, id: string, label: (match: RegExpMatchArray) => string, apply: (match: RegExpMatchArray) => void) => {
    const match = remaining.match(regex);
    if (!match) return;
    apply(match);
    conditions.push({ id: `${id}-${conditions.length}`, label: label(match), source: match[0] });
    remaining = remaining.replace(match[0], " ");
  };

  consume(/(?:得分率|总分率)?\s*(?:低于|小于|<)\s*(\d{1,3})\s*%?/i, "max", (match) => tx(locale, `得分率 < ${match[1]}%`, `Score < ${match[1]}%`), (match) => { maxPercent = clampNumber(Number(match[1]), 0, 101); });
  consume(/(?:得分率|总分率)?\s*(?:至少|不低于|大于等于|>=|≥)\s*(\d{1,3})\s*%?/i, "min", (match) => tx(locale, `得分率 ≥ ${match[1]}%`, `Score ≥ ${match[1]}%`), (match) => { minPercent = clampNumber(Number(match[1]), 0, 100); });
  consume(/不及格|未及格|fail(?:ed)?/i, "fail", () => tx(locale, "未及格", "Failed"), () => { pass = "fail"; });
  if (!pass) consume(/(?:^|\s)及格(?:\s|$)|pass(?:ed)?/i, "pass", () => tx(locale, "及格", "Passed"), () => { pass = "pass"; });
  consume(/低置信(?:度)?|low[\s-]*confidence/i, "confidence", () => tx(locale, "含低置信题次", "Has low-confidence items"), () => { lowConfidence = true; });
  consume(/待复核|待确认|未复核|未人工处理|pending[\s-]*review|not[\s-]*reviewed/i, "pending", () => tx(locale, "有未人工处理信号", "Has unreviewed signals"), () => { reviewState = "pending"; });
  if (!reviewState) consume(/已复核|已确认|教师已处理|reviewed|confirmed|teacher[\s-]*handled/i, "confirmed", () => tx(locale, "信号已由教师处理", "Signals handled by teacher"), () => { reviewState = "confirmed"; });
  if (!reviewState) consume(/无复核|无需复核|no[\s-]*review/i, "none", () => tx(locale, "无复核信号", "No review signals"), () => { reviewState = "none"; });
  consume(/低分优先|得分(?:率)?从低到高|score\s*(?:asc|low)/i, "sort-low", () => tx(locale, "低分优先", "Low score first"), () => { sort = "score_asc"; });
  if (!sort) consume(/高分优先|得分(?:率)?从高到低|score\s*(?:desc|high)/i, "sort-high", () => tx(locale, "高分优先", "High score first"), () => { sort = "score_desc"; });

  const terms = remaining.split(/[\s,，;；]+/).map(normalizeText).filter(Boolean);
  for (const term of terms) conditions.push({ id: `term-${conditions.length}`, label: tx(locale, `匹配：${term}`, `Match: ${term}`), source: term });
  return { minPercent, maxPercent, pass, lowConfidence, reviewState, sort, terms, conditions };
}

function matchesSemanticPlan(row: StudentAnalysisRow, plan: SemanticStudentPlan): boolean {
  const percent = row.student.percent;
  if (plan.minPercent !== null && (percent === null || percent < plan.minPercent)) return false;
  if (plan.maxPercent !== null && (percent === null || percent >= plan.maxPercent)) return false;
  if (plan.pass && !matchesPassFilter(percent, plan.pass)) return false;
  if (plan.lowConfidence && row.student.lowConfidenceCount === 0) return false;
  if (plan.reviewState && row.reviewState !== plan.reviewState) return false;
  const haystack = normalizeText(`${row.student.id} ${row.student.name}`);
  return plan.terms.every((term) => haystack.includes(term));
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
  if (sort === "score_asc") return nullable(left.student.percent, Number.POSITIVE_INFINITY) - nullable(right.student.percent, Number.POSITIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "score_desc") return nullable(right.student.percent, Number.NEGATIVE_INFINITY) - nullable(left.student.percent, Number.NEGATIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "confidence_asc") return nullable(normalizeConfidence(left.student.avgConfidence), Number.POSITIVE_INFINITY) - nullable(normalizeConfidence(right.student.avgConfidence), Number.POSITIVE_INFINITY) || compareStudents(left.student, right.student);
  if (sort === "review_desc") return right.requiredReviewCount - left.requiredReviewCount || right.disagreementCount - left.disagreementCount || compareStudents(left.student, right.student);
  return compareStudents(left.student, right.student);
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
function normalizeSortMode(value: string | null): SortMode { return value === "score_asc" || value === "score_desc" || value === "confidence_asc" || value === "review_desc" ? value : "student"; }
function normalizeConfidence(value: number | null | undefined): number | null { if (typeof value !== "number" || !Number.isFinite(value)) return null; return value > 1 ? value / 100 : value; }
function averageOrNull(values: number[]): number | null { return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null; }
function medianOrNull(values: number[]): number | null { if (!values.length) return null; const sorted = [...values].sort((a, b) => a - b); const middle = Math.floor(sorted.length / 2); return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2; }
function compareStudents(left: StudentSummary, right: StudentSummary): number { return left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" }) || left.id.localeCompare(right.id, undefined, { numeric: true, sensitivity: "base" }); }
function nullable(value: number | null, fallback: number): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function clampNumber(value: number, min: number, max: number): number { return Math.max(min, Math.min(max, value)); }
function normalizeText(value: string): string { return value.normalize("NFKC").trim().toLocaleLowerCase(); }
function tx(locale: Locale, zh: string, en: string): string { return locale === "en-US" ? en : zh; }
