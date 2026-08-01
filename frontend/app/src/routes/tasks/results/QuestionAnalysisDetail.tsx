import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Search, UserRound, X } from "lucide-react";
import { useEffect, useMemo, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  effectiveCorrectionScore,
  formatConfidence,
  formatPercent,
  formatScore,
  type QuestionEntry,
  type QuestionSummary,
  type ResultsModel,
} from "@/components/tasks/resultsModel";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { matchReviewItems, questionSearchItems } from "@/lib/reviewDetail";
import { ResultQuestionSidebar } from "@/routes/tasks/results/ResultQuestionSidebar";
import type { Correction, ProblemInfo } from "@/types";

interface CountedSignal {
  label: string;
  count: number;
}

interface RubricDimension {
  label: string;
  attempts: number;
  correct: number;
  averageScore: number;
}

export function QuestionAnalysisDetail({
  locale,
  taskId,
  questionId,
  model,
}: {
  locale: Locale;
  taskId: string;
  questionId: string;
  model: ResultsModel;
}) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const query = searchParams.get("question_q") ?? "";
  const smartSearch = useImeSafeQuery({ value: query, onCommit: updateQuery });
  const questionIndex = model.questions.findIndex((item) => item.id === questionId);
  const question = questionIndex >= 0 ? model.questions[questionIndex] : null;
  const root = `/tasks/${encodeURIComponent(taskId)}/results/questions`;
  const returnQuery = searchParams.get("return") ?? "";
  const studentContext = searchParams.get("student") ?? "";
  const studentReturnQuery = searchParams.get("student_return") ?? "";
  const listHref = returnQuery ? `${root}?${returnQuery}` : root;
  const detailParams = new URLSearchParams();
  if (returnQuery) detailParams.set("return", returnQuery);
  if (studentContext) detailParams.set("student", studentContext);
  if (studentReturnQuery) detailParams.set("student_return", studentReturnQuery);
  if (query) detailParams.set("question_q", query);
  const detailReturnSuffix = detailParams.size ? `?${detailParams.toString()}` : "";
  const studentContextHref = studentContext ? buildStudentContextHref(taskId, studentContext, questionId, studentReturnQuery) : "";
  const matches = useMemo(() => matchReviewItems(questionSearchItems(model.questions), query), [model.questions, query]);
  const visibleQuestions = useMemo(() => matches
    .map((match) => model.questions.find((item) => item.id === match.item.id))
    .filter((item): item is QuestionSummary => Boolean(item)), [matches, model.questions]);
  const filteredQuestionIndex = visibleQuestions.findIndex((item) => item.id === questionId);
  const previous = filteredQuestionIndex > 0 ? visibleQuestions[filteredQuestionIndex - 1] : null;
  const next = filteredQuestionIndex >= 0 && filteredQuestionIndex < visibleQuestions.length - 1 ? visibleQuestions[filteredQuestionIndex + 1] : null;

  function updateQuery(value: string) {
    const nextParams = new URLSearchParams(searchParams);
    if (value.trim()) nextParams.set("question_q", value);
    else nextParams.delete("question_q");
    setSearchParams(nextParams, { replace: true });
  }

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isEditableTarget(event.target)) return;
      if (event.key === "ArrowUp" && previous) {
        event.preventDefault();
        navigate(`${root}/${encodeURIComponent(previous.id)}${detailReturnSuffix}`);
      } else if (event.key === "ArrowDown" && next) {
        event.preventDefault();
        navigate(`${root}/${encodeURIComponent(next.id)}${detailReturnSuffix}`);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [detailReturnSuffix, navigate, next, previous, root]);

  if (!question) {
    return (
      <section className="rounded-[10px] border bg-card px-6 py-12 text-center">
        <h2 className="text-[18px] font-bold text-foreground">{tx(locale, "未找到该题", "Question not found")}</h2>
        <p className="mt-2 text-[13px] text-muted-foreground">{tx(locale, `当前正式结果中没有题目 ${questionId}。`, `Question ${questionId} is not included in these final results.`)}</p>
        <Link to={listHref} className="mt-5 inline-flex h-9 items-center gap-2 rounded-[8px] bg-primary px-4 text-[12px] font-semibold text-primary-foreground">
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />{tx(locale, "返回题目总览", "Back to question overview")}
        </Link>
      </section>
    );
  }

  const metrics = buildQuestionMetrics(question);
  const commonFeedback = buildCommonFeedback(question, locale);
  const rubricDimensions = buildRubricDimensions(question);
  const reviewSignals = buildReviewSignals(question, locale);
  const studentPreview = [...question.entries]
    .sort((left, right) => entryPercent(left) - entryPercent(right) || left.student.name.localeCompare(right.student.name, locale === "en-US" ? "en" : "zh-Hans-CN"))
    .slice(0, 5);
  const showTestMaterials = isProgrammingProblem(question.problem);

  const goToQuestion = (targetId: string) => {
    navigate(`${root}/${encodeURIComponent(targetId)}${detailReturnSuffix}`);
  };

  return (
    <section className="rounded-[10px] border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <Link to={listHref} className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-primary hover:underline">
              <ArrowLeft aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "返回题目分析总览", "Back to question overview")}
            </Link>
            {studentContextHref ? <Link to={studentContextHref} className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-primary hover:underline">{tx(locale, "返回当前学生详情", "Back to current student")}</Link> : null}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <h2 className="text-[22px] font-bold tracking-[-0.01em] text-foreground">{question.label}</h2>
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-muted-foreground">{question.type || "—"}</span>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            {filteredQuestionIndex >= 0
              ? tx(locale, `筛选结果中的第 ${filteredQuestionIndex + 1} / ${visibleQuestions.length} 题`, `Question ${filteredQuestionIndex + 1} of ${visibleQuestions.length}`)
              : tx(locale, `当前题目 · 共 ${model.questions.length} 题`, `Current question · ${model.questions.length} total`)}
          </p>
        </div>
        <QuestionStepButtons locale={locale} previous={previous} next={next} onSelect={goToQuestion} />
      </div>

      <div className="relative mt-4">
        <Search aria-hidden="true" className="pointer-events-none absolute left-4 top-4 h-4 w-4 text-muted-foreground" />
        <input
          value={smartSearch.draftValue}
          inputMode="search"
          onBlur={smartSearch.handleBlur}
          onCompositionStart={smartSearch.handleCompositionStart}
          onCompositionEnd={smartSearch.handleCompositionEnd}
          onChange={smartSearch.handleChange}
          placeholder={tx(locale, "SmarTAI 智能搜索：题号、题干、题型或知识点，例如“积分题”", "SmarTAI Smart Search: number, stem, type, or knowledge point")}
          aria-label={tx(locale, "SmarTAI 智能查找题目", "SmarTAI Smart question finder")}
          className="h-12 w-full rounded-[10px] border bg-background pl-11 pr-11 text-[13px] text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
        />
        {smartSearch.draftValue ? <button type="button" onClick={() => smartSearch.commitValue("")} aria-label={tx(locale, "清空题目筛选", "Clear question filter")} className="absolute right-2.5 top-2.5 inline-flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"><X aria-hidden="true" className="h-4 w-4" /></button> : null}
        {query && smartSearch.draftValue === query ? (
          <div className="absolute left-0 right-0 top-[52px] z-30 max-h-64 overflow-y-auto rounded-[8px] border bg-card p-1.5 shadow-lg">
            {matches.length ? matches.slice(0, 10).map((match) => (
              <button key={match.item.id} type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => goToQuestion(match.item.id)} className="flex w-full items-center gap-3 rounded-[6px] px-3 py-2 text-left hover:bg-muted">
                <span className="min-w-14 text-[12px] font-bold text-foreground">{match.item.primary}</span>
                <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold", match.kind === "exact" ? "bg-emerald-100 text-emerald-700" : "bg-blue-50 text-primary")}>{match.kind === "exact" ? tx(locale, "完全匹配", "Exact") : tx(locale, "相关匹配", "Related")}</span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{match.item.secondary || "—"}</span>
              </button>
            )) : <p className="px-3 py-5 text-center text-[11px] text-muted-foreground">{tx(locale, "没有匹配题目；清空筛选即可恢复全部。", "No question matched; clear the filter to restore all questions.")}</p>}
          </div>
        ) : null}
      </div>

      <div className="mt-2 grid gap-0.5 text-[10px] leading-4 text-muted-foreground">
        <p>{tx(locale, "搜索只筛选题目；输入中文时在选词完成后应用。", "Search filters questions only and waits for IME composition.")}</p>
        <p>{tx(locale, "输入框聚焦时方向键只编辑文本；退出输入框后，↑/↓ 切换题目。", "Arrow keys edit text while the input is focused; after leaving it, ↑/↓ switch questions.")}</p>
      </div>

      <div className="mt-4 grid items-start gap-4 lg:grid-cols-[132px_minmax(0,1fr)]">
        <ResultQuestionSidebar
          locale={locale}
          questions={visibleQuestions}
          activeId={question.id}
          onSelect={goToQuestion}
          stateForQuestion={(item) => item.reviewCount > 0 ? "warning" : "ready"}
        />
        <div className="min-w-0">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-6">
        <DetailMetric label={tx(locale, "作答人数", "Responses")} value={String(question.count)} tone="primary" />
        <DetailMetric label={tx(locale, "平均分", "Mean")} value={`${formatScore(question.avgScore)} / ${formatScore(question.maxScore)}`} tone="accent" />
        <DetailMetric label={tx(locale, "中位数", "Median")} value={formatScore(metrics.median)} tone="primary" />
        <DetailMetric label={tx(locale, "最低 / 最高", "Min / max")} value={`${formatScore(question.minScore)} / ${formatScore(question.maxObservedScore)}`} tone="warning" />
        <DetailMetric label={tx(locale, "平均置信度", "Mean confidence")} value={formatConfidence(metrics.avgConfidence)} tone="accent" />
        <DetailMetric label={tx(locale, "必审 / 分歧题次", "Review / disagreement")} value={`${metrics.requiredReviewCount} / ${metrics.disagreementCount}`} tone="danger" />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <MaterialPanel title={tx(locale, "题干", "Question Text")} source={fieldSource(question.problem, "stem", locale)}>
          {question.stem ? <MarkdownMath className="text-[13px] leading-6 text-foreground">{question.stem}</MarkdownMath> : <MissingText locale={locale} />}
        </MaterialPanel>
        <MaterialPanel title={tx(locale, "评分标准", "Rubric")} source={fieldSource(question.problem, "criterion", locale)}>
          {question.criterion ? <MarkdownMath className="text-[13px] leading-6 text-foreground">{question.criterion}</MarkdownMath> : <MissingText locale={locale} />}
        </MaterialPanel>
        <MaterialPanel className="xl:col-span-2" title={tx(locale, "标答 / 参考答案", "Reference answer")} source={fieldSource(question.problem, "reference_answer", locale)}>
          {question.problem?.reference_answer ? <MarkdownMath className="text-[13px] leading-6 text-foreground">{question.problem.reference_answer}</MarkdownMath> : <MissingText locale={locale} />}
        </MaterialPanel>
        {showTestMaterials ? (
          <MaterialPanel className="xl:col-span-2" title={tx(locale, "代码 / 测试资料", "Code / test materials")} source={fieldSource(question.problem, "test_cases", locale)}>
            <TestMaterialSummary locale={locale} problem={question.problem} />
          </MaterialPanel>
        ) : null}
      </div>

      <div className="mt-4">
        <EvidencePanel title={tx(locale, "Rubric 维度表现", "Performance by Rubric Criterion")} subtitle={tx(locale, "按批改步骤描述聚合", "Aggregated from grading-step descriptions")}>
          {rubricDimensions.length ? <div className="mt-3 grid gap-2 lg:grid-cols-2">{rubricDimensions.map((dimension) => (
            <div key={dimension.label} className="rounded-[7px] bg-muted/60 px-3 py-2">
              <div className="flex items-start justify-between gap-3 text-[11px]"><strong className="min-w-0 break-words leading-4 text-foreground">{dimension.label}</strong><span className="shrink-0 font-semibold text-primary">{formatScore(dimension.averageScore)} {tx(locale, "平均分", "mean")}</span></div>
              <p className="mt-1 text-[10px] text-muted-foreground">{tx(locale, `${dimension.correct}/${dimension.attempts} 次标记为正确`, `${dimension.correct}/${dimension.attempts} marked correct`)}</p>
            </div>
          ))}</div> : <PanelEmpty text={tx(locale, "当前结果没有步骤级 rubric 数据。", "No step-level rubric data is available.")} />}
        </EvidencePanel>
        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <EvidencePanel title={tx(locale, "易错点与常见问题（有据）", "Common issues (evidence-backed)")} subtitle={tx(locale, "按重复评分反馈计数，不由前端生成主题", "Counts repeated grading feedback; no invented topic")}>
            <SignalList locale={locale} signals={commonFeedback} empty={tx(locale, "没有重复反馈可形成摘要。", "No repeated feedback to summarize.")} />
          </EvidencePanel>
          <EvidencePanel title={tx(locale, "复核与分歧信号", "Review & disagreement signals")} subtitle={tx(locale, `基于 ${question.count} 份真实作答`, `Based on ${question.count} real responses`)}>
            <SignalList locale={locale} signals={reviewSignals} empty={tx(locale, "没有正式复核或分歧信号。", "No formal review or disagreement signal.")} />
          </EvidencePanel>
        </div>
      </div>

      <div className="mt-4 rounded-[9px] border">
        <div className="flex flex-wrap items-end justify-between gap-3 border-b px-4 py-3">
          <div>
            <h3 className="text-[14px] font-bold text-foreground">{tx(locale, "学生表现摘要", "Student performance preview")}</h3>
            <p className="mt-0.5 text-[11px] text-muted-foreground">{tx(locale, "得分率从低到高只显示 5 位；完整答案进入学生详情。", "Lowest score percentages first; only 5 students are shown. Open a student's details to view the full response.")}</p>
          </div>
          <span className="text-[10px] text-muted-foreground">{tx(locale, `共 ${question.entries.length} 位`, `${question.entries.length} students`)}</span>
        </div>
        {studentPreview.length ? (
          <div className="divide-y">
            {studentPreview.map((entry) => <StudentPreview key={entry.student.id} locale={locale} taskId={taskId} question={question} entry={entry} />)}
          </div>
        ) : <PanelEmpty text={tx(locale, "当前题目没有学生结果。", "No student result is available for this question.")} />}
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-[9px] bg-muted/40 px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Link to={listHref} className="inline-flex h-9 items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-foreground hover:bg-muted">
            <ArrowLeft aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "题目总览", "Question overview")}
          </Link>
          {studentContextHref ? <Link to={studentContextHref} className="inline-flex h-9 items-center rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-primary hover:bg-muted">{tx(locale, "当前学生详情", "Current student")}</Link> : null}
        </div>
        <QuestionStepButtons locale={locale} previous={previous} next={next} onSelect={goToQuestion} />
      </div>
        </div>
      </div>
    </section>
  );
}

function QuestionStepButtons({ locale, previous, next, onSelect }: { locale: Locale; previous: QuestionSummary | null; next: QuestionSummary | null; onSelect: (id: string) => void }) {
  return (
    <div className="flex items-center gap-2">
      <button type="button" disabled={!previous} onClick={() => previous && onSelect(previous.id)} className="inline-flex h-9 items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-35">
        <ArrowUp aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "上一题", "Previous")}
      </button>
      <button type="button" disabled={!next} onClick={() => next && onSelect(next.id)} className="inline-flex h-9 items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-35">
        {tx(locale, "下一题", "Next")}<ArrowDown aria-hidden="true" className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function DetailMetric({ label, value, tone }: { label: string; value: string; tone: "primary" | "accent" | "warning" | "danger" }) {
  return (
    <div className="rounded-[9px] border px-3 py-3">
      <strong className={cn("text-[18px] leading-6", tone === "primary" && "text-primary", tone === "accent" && "text-teal-500", tone === "warning" && "text-amber-500", tone === "danger" && "text-rose-500")}>{value}</strong>
      <span className="mt-1 block text-[10px] font-medium text-muted-foreground">{label}</span>
    </div>
  );
}

function MaterialPanel({ className, title, source, children }: { className?: string; title: string; source: string; children: ReactNode }) {
  return (
    <section className={cn("min-w-0 rounded-[9px] border px-4 py-3.5", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-bold text-foreground">{title}</h3>
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-muted-foreground">{source}</span>
      </div>
      <div className="mt-3 min-w-0 overflow-x-auto rounded-[7px] bg-muted/50 px-3 py-2.5">{children}</div>
    </section>
  );
}

function EvidencePanel({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-[9px] border px-4 py-3.5">
      <h3 className="text-[13px] font-bold text-foreground">{title}</h3>
      <p className="mt-0.5 text-[10px] text-muted-foreground">{subtitle}</p>
      {children}
    </section>
  );
}

function SignalList({ signals, empty }: { locale: Locale; signals: CountedSignal[]; empty: string }) {
  if (!signals.length) return <PanelEmpty text={empty} />;
  return (
    <div className="mt-3 grid gap-2">
      {signals.slice(0, 4).map((signal) => (
        <div key={signal.label} className="flex items-start justify-between gap-3 rounded-[7px] bg-muted/60 px-3 py-2 text-[11px]">
          <span className="min-w-0 leading-4 text-foreground">{signal.label}</span>
          <span className="shrink-0 rounded-full bg-background px-2 py-0.5 font-semibold text-primary">×{signal.count}</span>
        </div>
      ))}
    </div>
  );
}

function StudentPreview({ locale, taskId, question, entry }: { locale: Locale; taskId: string; question: QuestionSummary; entry: QuestionEntry }) {
  const score = effectiveCorrectionScore(entry.correction);
  const percent = entry.correction.max_score > 0 ? (score / entry.correction.max_score) * 100 : null;
  const href = `/tasks/${encodeURIComponent(taskId)}/results/students/${encodeURIComponent(entry.student.id)}?question=${encodeURIComponent(question.id)}#question-${encodeURIComponent(question.id)}`;
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(0,1.4fr)_100px_100px_120px_auto] sm:items-center">
      <div className="min-w-0">
        <strong className="block truncate text-[12px] text-foreground">{entry.student.name} · {entry.student.id}</strong>
        <span className="mt-0.5 block truncate text-[10px] text-muted-foreground">{entry.correction.teacher_comment || entry.correction.comment || tx(locale, "暂无评语", "No feedback")}</span>
      </div>
      <span className="text-[11px] font-semibold text-primary">{formatScore(score)} / {formatScore(entry.correction.max_score)} · {formatPercent(percent)}</span>
      <span className="text-[11px] text-muted-foreground">{formatConfidence(entry.correction.confidence)}</span>
      <span className={cn("w-fit rounded-full px-2.5 py-1 text-[10px] font-semibold", entry.correction.review_status === "confirmed" ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-600")}>{entry.correction.review_status === "confirmed" ? tx(locale, "已确认", "Confirmed") : tx(locale, "无必审确认", "No required confirmation")}</span>
      <Link to={href} className="inline-flex h-8 w-fit items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[10px] font-semibold text-foreground hover:bg-muted">
        <UserRound aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "学生详情", "Student detail")}<ArrowRight aria-hidden="true" className="h-3 w-3" />
      </Link>
    </div>
  );
}

function TestMaterialSummary({ locale, problem }: { locale: Locale; problem?: ProblemInfo }) {
  const testCases = problem?.test_cases ?? [];
  if (!testCases.length) return <MissingText locale={locale} />;
  return (
    <div className="grid gap-2">
      <p className="text-[11px] font-semibold text-foreground">{tx(locale, `${testCases.length} 个测试样例`, `${testCases.length} test cases`)}</p>
      {testCases.slice(0, 2).map((testCase, index) => (
        <div key={`${testCase.input}-${index}`} className="rounded-[6px] border bg-background px-2.5 py-2 text-[10px] text-muted-foreground">
          <span className="font-semibold text-foreground">#{index + 1}</span> · {tx(locale, "输入", "Input")} {testCase.input || "—"} · {tx(locale, "期望", "Expected")} {testCase.expected_output || "—"}
        </div>
      ))}
      {problem?.solution_code ? <pre className="max-h-20 overflow-auto rounded-[6px] bg-slate-950 p-2 text-[10px] text-slate-100">{problem.solution_code}</pre> : null}
    </div>
  );
}

function isProgrammingProblem(problem?: ProblemInfo): boolean {
  const type = problem?.type ?? "";
  return Boolean(
    problem?.solution_code?.trim()
    || (problem?.test_cases?.length ?? 0) > 0
    || /编程|程序|代码|program|coding|code/i.test(type),
  );
}

function MissingText({ locale }: { locale: Locale }) {
  return <p className="text-[12px] text-muted-foreground">{tx(locale, "当前正式结果未提供该资料。", "This material is not included in the final results.")}</p>;
}

function PanelEmpty({ text }: { text: string }) {
  return <p className="mt-3 flex min-h-14 items-center justify-center rounded-[7px] bg-muted/50 px-3 text-center text-[11px] text-muted-foreground">{text}</p>;
}

function buildQuestionMetrics(question: QuestionSummary) {
  const scores = question.entries.map((entry) => effectiveCorrectionScore(entry.correction)).filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
  const confidences = question.entries.map((entry) => normalizeConfidence(entry.correction.confidence)).filter((value): value is number => value !== null);
  const requiredReviewCount = question.entries.filter((entry) => correctionNeedsFormalReview(entry.correction)).length;
  const disagreementCount = question.entries.filter((entry) => correctionHasDisagreement(entry.correction)).length;
  return {
    median: median(scores),
    avgConfidence: averageOrNull(confidences),
    requiredReviewCount,
    disagreementCount,
  };
}

function buildCommonFeedback(question: QuestionSummary, locale: Locale): CountedSignal[] {
  const counts = new Map<string, number>();
  for (const entry of question.entries) {
    const feedback = String(entry.correction.teacher_comment || entry.correction.comment || "").trim();
    if (!feedback) continue;
    counts.set(feedback, (counts.get(feedback) ?? 0) + 1);
  }
  return Array.from(counts, ([label, count]) => ({ label, count }))
    .filter((signal) => signal.count > 1 || counts.size <= 3)
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label, locale === "en-US" ? "en" : "zh-Hans-CN"))
    .slice(0, 4);
}

function buildRubricDimensions(question: QuestionSummary): RubricDimension[] {
  const dimensions = new Map<string, { scores: number[]; correct: number }>();
  for (const entry of question.entries) {
    for (const step of entry.correction.steps ?? []) {
      const label = String(step.desc || `Step ${step.step_no}`).trim();
      if (!label) continue;
      const current = dimensions.get(label) ?? { scores: [], correct: 0 };
      if (Number.isFinite(step.score)) current.scores.push(step.score);
      if (step.is_correct) current.correct += 1;
      dimensions.set(label, current);
    }
  }
  return Array.from(dimensions, ([label, value]) => ({
    label,
    attempts: value.scores.length,
    correct: value.correct,
    averageScore: value.scores.length ? value.scores.reduce((sum, score) => sum + score, 0) / value.scores.length : 0,
  })).sort((left, right) => left.label.localeCompare(right.label, undefined, { numeric: true }));
}

function buildReviewSignals(question: QuestionSummary, locale: Locale): CountedSignal[] {
  const counts = new Map<string, number>();
  const add = (label: string) => counts.set(label, (counts.get(label) ?? 0) + 1);
  for (const entry of question.entries) {
    const confidence = normalizeConfidence(entry.correction.confidence);
    if (confidence !== null && confidence < 0.65) add(tx(locale, "低置信度", "Low confidence"));
    if (entry.correction.requires_human_review) add(tx(locale, "系统标记需人工复核", "System requested human review"));
    for (const reason of entry.correction.review_reasons ?? []) {
      if (reason !== "high_indecisiveness" && reason !== "score_spread_high") add(reviewReasonLabel(reason, locale));
    }
    if (correctionHasDisagreement(entry.correction)) add(tx(locale, "专家分歧 / 分差较大", "Model disagreement / score spread"));
  }
  return Array.from(counts, ([label, count]) => ({ label, count })).sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

function correctionNeedsFormalReview(correction: Correction): boolean {
  const confidence = normalizeConfidence(correction.confidence);
  return (confidence !== null && confidence < 0.65)
    || correction.requires_human_review
    || correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")
    || correction.synthesis_method === "all_failed"
    || correction.synthesis_method === "quota_exhausted"
    || correctionHasDisagreement(correction);
}

function correctionHasDisagreement(correction: Correction): boolean {
  if (correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")) return true;
  const scores = correction.expert_results?.map((result) => Number(result.score)).filter((value) => Number.isFinite(value)) ?? [];
  return scores.length > 1 && correction.max_score > 0 && Math.max(...scores) - Math.min(...scores) > Math.max(1, correction.max_score * 0.25);
}

function reviewReasonLabel(reason: string, locale: Locale): string {
  const labels: Record<string, [string, string]> = {
    low_confidence: ["低置信度", "Low confidence"],
    high_indecisiveness: ["专家意见分歧", "Model disagreement"],
    score_spread_high: ["专家分差较大", "Large score spread across models"],
    parse_failed: ["解析失败", "Parsing failed"],
    quota_exhausted: ["模型额度失败", "Model quota failure"],
  };
  const label = labels[reason];
  return label ? label[locale === "en-US" ? 1 : 0] : reason.replaceAll("_", " ");
}

function fieldSource(problem: ProblemInfo | undefined, field: "stem" | "criterion" | "reference_answer" | "test_cases", locale: Locale): string {
  if (!problem) return tx(locale, "未提供", "Unavailable");
  if (field !== "stem") {
    const imported = problem.material_provenance?.[field as keyof typeof problem.material_provenance];
    if (imported?.source_filename) return tx(locale, `资料导入 · ${imported.source_filename}`, `Imported · ${imported.source_filename}`);
    const generated = problem.ai_completion_provenance?.[field as keyof typeof problem.ai_completion_provenance];
    if (generated) return tx(locale, "SmarTAI 补全 · 教师已确认版本", "SmarTAI completion · teacher-confirmed version");
  }
  const value = field === "stem" ? problem.stem : field === "criterion" ? problem.criterion : field === "reference_answer" ? problem.reference_answer : problem.test_cases;
  if (Array.isArray(value) ? value.length > 0 : Boolean(value)) return tx(locale, "题目识别 / 教师维护", "Recognition / teacher maintained");
  return tx(locale, "未提供", "Unavailable");
}

function buildStudentContextHref(taskId: string, studentId: string, questionId: string, serialized: string): string {
  const params = new URLSearchParams(serialized);
  if (!params.has("question")) params.set("question", questionId);
  const query = params.toString();
  return `/tasks/${encodeURIComponent(taskId)}/results/students/${encodeURIComponent(studentId)}${query ? `?${query}` : ""}#question-${encodeURIComponent(questionId)}`;
}

function entryPercent(entry: QuestionEntry): number {
  const max = Number(entry.correction.max_score);
  return max > 0 ? (effectiveCorrectionScore(entry.correction) / max) * 100 : Number.POSITIVE_INFINITY;
}

function normalizeConfidence(value: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value > 1 ? value / 100 : value;
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const middle = Math.floor(values.length / 2);
  return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

function averageOrNull(values: number[]): number | null {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}
