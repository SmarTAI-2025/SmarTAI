import { confidenceValue } from "@/lib/confidencePolicy";
import { correctionReviewLabels } from "@/components/tasks/resultsReviewModel";
import { ResultQuestionQuery, useResultQuestionFilter } from "@/components/tasks/ResultQuestionQuery";
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, UserRound } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  hasReviewSignal as correctionNeedsFormalReview,
  hasExpertDisagreement,
  effectiveCorrectionScore,
  formatCorrectionScoreSource,
  formatConfidence,
  formatPercent,
  formatScore,
  type QuestionEntry,
  type QuestionSummary,
  type ResultsModel,
} from "@/components/tasks/resultsModel";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { normalizeCodeLineBreaks } from "@/components/ui/SyntaxHighlightedCode";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
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
  const { filter: questionFilter, visibleQuestions } = useResultQuestionFilter({ taskId, questions: model.questions, locale });
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

      <ResultQuestionQuery className="mt-4" locale={locale} taskId={taskId} filter={questionFilter}
        questions={visibleQuestions} onSelect={goToQuestion} />

      <div className="mt-2 grid gap-0.5 text-[10px] leading-4 text-muted-foreground">
        <p>{tx(locale, "搜索只筛选题目；输入中文时在选词完成后应用。", "Search filters questions only and waits for IME composition.")}</p>
        <p>{tx(locale, "退出输入框后，可用 ↑/↓ 切换题目。", "Outside the search box, use ↑/↓ to switch questions.")}</p>
      </div>

      <div className="mt-4 grid items-start gap-4 lg:grid-cols-[clamp(180px,16vw,240px)_minmax(0,1fr)]">
        <ResultQuestionSidebar
          locale={locale}
          questions={visibleQuestions}
          activeId={question.id}
          onSelect={goToQuestion}
          stateForQuestion={(item) => item.entries.some(({ correction }) => correctionNeedsFormalReview(correction)) ? "warning" : "ready"}
        />
        <div className="min-w-0">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-6">
        <DetailMetric label={tx(locale, "作答人数", "Responses")} value={String(question.count)} tone="primary" />
        <DetailMetric label={tx(locale, "平均分", "Mean")} value={`${formatScore(question.avgScore, question.maxScore)} / ${String(question.maxScore)}`} tone="accent" />
        <DetailMetric label={tx(locale, "中位数", "Median")} value={formatScore(metrics.median)} tone="primary" />
        <DetailMetric label={tx(locale, "最低 / 最高", "Min / max")} value={`${formatScore(question.minScore, question.maxScore)} / ${formatScore(question.maxObservedScore, question.maxScore)}`} tone="warning" />
        <DetailMetric label={tx(locale, "平均置信度", "Mean confidence")} value={formatConfidence(metrics.avgConfidence)} tone="accent" />
        <DetailMetric label={tx(locale, "复核信号 / 分歧题次", "Review signals / disagreement")} value={`${metrics.requiredReviewCount} / ${metrics.disagreementCount}`} tone="danger" />
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
            <TestMaterialSummary key={question.id} locale={locale} problem={question.problem} />
          </MaterialPanel>
        ) : null}
      </div>

      <div className="mt-4">
        <EvidencePanel title={tx(locale, "评分项表现", "Performance by scoring criterion")} subtitle={tx(locale, "各评分项的平均得分", "Average score by criterion")}>
          {rubricDimensions.length ? <div className="mt-3 grid gap-2 lg:grid-cols-2">{rubricDimensions.map((dimension) => (
            <div key={dimension.label} className="rounded-[7px] bg-muted/60 px-3 py-2">
              <div className="flex items-start justify-between gap-3 text-[11px]"><strong className="min-w-0 break-words leading-4 text-foreground">{dimension.label}</strong><span className="shrink-0 font-semibold text-primary">{formatScore(dimension.averageScore)} {tx(locale, "平均分", "mean")}</span></div>
              <p className="mt-1 text-[10px] text-muted-foreground">{tx(locale, `${dimension.correct}/${dimension.attempts} 次标记为正确`, `${dimension.correct}/${dimension.attempts} marked correct`)}</p>
            </div>
          ))}</div> : <PanelEmpty text={tx(locale, "暂无分项评分。", "No scores by criterion are available.")} />}
        </EvidencePanel>
        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <EvidencePanel title={tx(locale, "常见问题", "Common issues")} subtitle={tx(locale, "出现次数较多的评分反馈", "Frequently occurring grading feedback")}>
            <SignalList locale={locale} signals={commonFeedback} empty={tx(locale, "没有重复反馈可形成摘要。", "No repeated feedback to summarize.")} />
          </EvidencePanel>
          <EvidencePanel title={tx(locale, "复核与分歧信号", "Review & disagreement signals")} subtitle={tx(locale, `基于 ${question.count} 份作答`, `Based on ${question.count} responses`)}>
            <SignalList locale={locale} signals={reviewSignals} empty={tx(locale, "暂无需要关注的问题。", "No issues need attention.")} />
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
  const percent = score !== null && entry.correction.max_score > 0
    ? (score / entry.correction.max_score) * 100
    : null;
  const href = `/tasks/${encodeURIComponent(taskId)}/results/students/${encodeURIComponent(entry.student.id)}?question=${encodeURIComponent(question.id)}#question-${encodeURIComponent(question.id)}`;
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(0,1.4fr)_100px_100px_120px_auto] sm:items-center">
      <div className="min-w-0">
        <strong className="block truncate text-[12px] text-foreground">{entry.student.name} · {entry.student.id}</strong>
        <span className="mt-0.5 block truncate text-[10px] text-muted-foreground">{entry.correction.teacher_comment || entry.correction.comment || tx(locale, "暂无评语", "No feedback")}</span>
      </div>
      <span className="text-[11px] font-semibold text-primary">{formatScore(score, entry.correction.max_score)} / {String(entry.correction.max_score)} · {formatPercent(percent)}</span>
      <span className="text-[11px] text-muted-foreground">{formatConfidence(entry.correction.confidence)}</span>
      <span className="w-fit rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-600">{formatCorrectionScoreSource(entry.correction, locale)}</span>
      <Link to={href} className="inline-flex h-8 w-fit items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[10px] font-semibold text-foreground hover:bg-muted">
        <UserRound aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "学生详情", "Student detail")}<ArrowRight aria-hidden="true" className="h-3 w-3" />
      </Link>
    </div>
  );
}

function TestMaterialSummary({ locale, problem }: { locale: Locale; problem?: ProblemInfo }) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const testCases = problem?.test_cases ?? [];
  const preview = !expanded && testCases.length > 2;
  return (
    <div className="grid gap-2">
      {testCases.length ? <p className="text-[11px] font-semibold text-foreground">{preview
        ? tx(locale, `前 2 项 / 共 ${testCases.length} 项`, `First 2 of ${testCases.length} test cases`)
        : tx(locale, `共 ${testCases.length} 个测试样例`, `${testCases.length} test cases`)}</p> : <MissingText locale={locale} />}
      <div id={listId} className="grid min-w-0 gap-2">
        {(preview ? testCases.slice(0, 2) : testCases).map((testCase, index) => {
          const fields: [string, string | unknown[] | null | undefined][] = testCase.io_mode === "function"
            ? [[tx(locale, "函数名", "Function"), testCase.function_name],
              [tx(locale, "调用参数", "Arguments"), testCase.function_args],
              [tx(locale, "预期返回", "Expected return"), testCase.expected_return]]
            : [[tx(locale, "输入", "Input"), testCase.input],
              [tx(locale, "期望输出", "Expected output"), testCase.expected_output]];
          return (
            <article key={index} aria-label={tx(locale, `测试样例 ${index + 1}`, `Test case ${index + 1}`)} className="min-w-0 rounded-[6px] border bg-background px-2.5 py-2 text-[10px] text-muted-foreground">
              <p className="break-words font-semibold text-foreground">#{index + 1}{testCase.title ? ` · ${testCase.title}` : ""}</p>
              <dl className="mt-1 grid min-w-0 gap-2">
                {fields.map(([label, value]) => (
                  <div key={label} className="min-w-0">
                    <dt className="font-semibold">{label}</dt>
                    <dd><pre className="max-h-48 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-foreground">{value == null
                      ? tx(locale, "未提供", "Not provided")
                      : Array.isArray(value) ? JSON.stringify(value, null, 2) : value === "" ? '""' : value}</pre></dd>
                  </div>
                ))}
              </dl>
              {testCase.description ? <p className="mt-2 whitespace-pre-wrap [overflow-wrap:anywhere]">{testCase.description}</p> : null}
            </article>
          );
        })}
      </div>
      {testCases.length > 2 ? <button type="button" aria-expanded={expanded} aria-controls={listId} onClick={() => setExpanded(value => !value)} className="w-fit rounded-[6px] border px-3 py-1.5 text-[11px] font-semibold text-primary hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
        {expanded ? tx(locale, "收起", "Show fewer") : tx(locale, `查看全部 ${testCases.length} 项`, `View all ${testCases.length} test cases`)}
      </button> : null}
      {problem?.solution_code ? <pre className="max-h-20 overflow-auto whitespace-pre rounded-[6px] bg-slate-950 p-2 text-[10px] text-slate-100">{normalizeCodeLineBreaks(problem.solution_code)}</pre> : null}
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
  const scores = question.entries
    .map((entry) => effectiveCorrectionScore(entry.correction))
    .filter((value): value is number => value !== null)
    .sort((a, b) => a - b);
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
    if (!correctionNeedsFormalReview(entry.correction)) continue;
    for (const label of correctionReviewLabels(entry.correction, locale)) add(label);
  }
  return Array.from(counts, ([label, count]) => ({ label, count })).sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

function correctionHasDisagreement(correction: Correction): boolean {
  return correctionNeedsFormalReview(correction) && hasExpertDisagreement(correction);
}

function fieldSource(problem: ProblemInfo | undefined, field: "stem" | "criterion" | "reference_answer" | "test_cases", locale: Locale): string {
  if (!problem) return tx(locale, "未提供", "Unavailable");
  if (field !== "stem") {
    const imported = problem.material_provenance?.[field as keyof typeof problem.material_provenance];
    if (imported?.source_filename) return tx(locale, `资料导入 · ${imported.source_filename}`, `Imported · ${imported.source_filename}`);
    const generated = problem.ai_completion_provenance?.[field as keyof typeof problem.ai_completion_provenance];
    // Provenance identifies the source, not a confirmation of this material version.
    if (generated) return tx(locale, "SmarTAI 补全", "SmarTAI completion");
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
  const score = effectiveCorrectionScore(entry.correction);
  return score !== null && max > 0
    ? (score / max) * 100
    : Number.POSITIVE_INFINITY;
}

function normalizeConfidence(value: number | null | undefined): number | null {
  return confidenceValue(value);
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
