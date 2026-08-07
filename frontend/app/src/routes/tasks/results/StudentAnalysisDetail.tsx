import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  aiCorrectionScore,
  correctionScoreSource,
  effectiveCorrectionScore,
  formatCorrectionScoreSource,
  formatConfidence,
  formatPercent,
  formatScore,
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "@/components/tasks/resultsModel";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import {
  matchReviewItems,
  questionSearchItems,
  type ReviewSearchMatch,
} from "@/lib/reviewDetail";
import { ResultQuestionSidebar, type ResultQuestionState } from "@/routes/tasks/results/ResultQuestionSidebar";
import type { Correction } from "@/types";

/** A05: read-only formal-result detail with independent student and question dimensions. */
export function StudentAnalysisDetail({ locale, taskId, studentId, model }: { locale: Locale; taskId: string; studentId: string; model: ResultsModel }) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const questionQuery = searchParams.get("question_q") ?? "";
  const selectedQuestionId = searchParams.get("question") ?? "";
  const returnQuery = searchParams.get("return") ?? "";
  const student = model.students.find((item) => item.id === studentId) ?? null;
  const questionMatches = useMemo(() => matchReviewItems(questionSearchItems(model.questions), questionQuery), [model.questions, questionQuery]);
  const visibleQuestions = useMemo(() => questionMatches.map((match) => model.questions.find((item) => item.id === match.item.id)).filter((item): item is QuestionSummary => Boolean(item)), [model.questions, questionMatches]);
  const studentIndex = model.students.findIndex((item) => item.id === studentId);
  const previousStudent = studentIndex > 0 ? model.students[studentIndex - 1] : null;
  const nextStudent = studentIndex >= 0 && studentIndex < model.students.length - 1 ? model.students[studentIndex + 1] : null;
  const studentsRoot = `/tasks/${encodeURIComponent(taskId)}/results/students`;
  const listHref = returnQuery ? `${studentsRoot}?${returnQuery}` : studentsRoot;
  const [activeQuestionId, setActiveQuestionId] = useState<string | null>(() => (
    selectedQuestionId && visibleQuestions.some((item) => item.id === selectedQuestionId)
      ? selectedQuestionId
      : visibleQuestions[0]?.id ?? null
  ));
  const activeQuestionIndex = visibleQuestions.findIndex((item) => item.id === activeQuestionId);
  const previousQuestion = activeQuestionIndex > 0 ? visibleQuestions[activeQuestionIndex - 1] : null;
  const nextQuestion = activeQuestionIndex >= 0 && activeQuestionIndex < visibleQuestions.length - 1 ? visibleQuestions[activeQuestionIndex + 1] : null;

  const buildHref = useCallback((nextStudentId: string, nextQuestionId: string | null, includeQuestionHash = true) => {
    const next = new URLSearchParams(searchParams);
    next.delete("student_q");
    if (nextQuestionId) next.set("question", nextQuestionId);
    else next.delete("question");
    const serialized = next.toString();
    const hash = includeQuestionHash && nextQuestionId ? `#question-${encodeURIComponent(nextQuestionId)}` : "";
    return `${studentsRoot}/${encodeURIComponent(nextStudentId)}${serialized ? `?${serialized}` : ""}${hash}`;
  }, [searchParams, studentsRoot]);

  const setFilter = (key: "question_q", value: string) => {
    const next = new URLSearchParams(searchParams);
    next.delete("student_q");
    if (value.trim()) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true });
  };

  const goToStudent = useCallback((nextStudentId: string) => {
    navigate(buildHref(nextStudentId, activeQuestionId, false));
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "auto" }));
  }, [activeQuestionId, buildHref, navigate]);

  const scrollToQuestion = useCallback((nextQuestionId: string) => {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("question", nextQuestionId);
    setSearchParams(nextParams, { replace: true });
    setActiveQuestionId(nextQuestionId);
    window.requestAnimationFrame(() => {
      document.getElementById(`question-${nextQuestionId}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    if (selectedQuestionId && visibleQuestions.some((item) => item.id === selectedQuestionId)) {
      setActiveQuestionId(selectedQuestionId);
      return;
    }
    setActiveQuestionId((current) => visibleQuestions.some((item) => item.id === current) ? current : visibleQuestions[0]?.id ?? null);
  }, [selectedQuestionId, visibleQuestions]);

  useEffect(() => {
    if (!visibleQuestions.length) return undefined;
    const syncActiveQuestion = () => {
      const atDocumentEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 8;
      if (atDocumentEnd) {
        setActiveQuestionId(visibleQuestions[visibleQuestions.length - 1].id);
        return;
      }
      let activeId = visibleQuestions[0].id;
      for (const item of visibleQuestions) {
        const element = document.getElementById(`question-${item.id}`);
        if (element && element.getBoundingClientRect().top <= 112) activeId = item.id;
      }
      setActiveQuestionId(activeId);
    };
    syncActiveQuestion();
    window.addEventListener("scroll", syncActiveQuestion, { passive: true });
    return () => window.removeEventListener("scroll", syncActiveQuestion);
  }, [visibleQuestions]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isEditableTarget(event.target)) return;
      if (event.key === "ArrowLeft" && previousStudent) {
        event.preventDefault();
        goToStudent(previousStudent.id);
      } else if (event.key === "ArrowRight" && nextStudent) {
        event.preventDefault();
        goToStudent(nextStudent.id);
      } else if (event.key === "ArrowUp" && previousQuestion) {
        event.preventDefault();
        scrollToQuestion(previousQuestion.id);
      } else if (event.key === "ArrowDown" && nextQuestion) {
        event.preventDefault();
        scrollToQuestion(nextQuestion.id);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [goToStudent, nextQuestion, nextStudent, previousQuestion, previousStudent, scrollToQuestion]);

  if (!student) {
    return <DetailState locale={locale} title={tx(locale, `当前正式结果中没有学生 ${studentId}。`, `Student ${studentId} is not included in these final results.`)} href={listHref} />;
  }
  const pendingReviewCount = student.corrections.filter((correction) => {
    if (!correctionNeedsFormalReview(correction)) return false;
    const source = correctionScoreSource(correction);
    return source === "ai_untouched" || source === "hard_failure";
  }).length;
  const validCorrectionCount = student.corrections.filter((correction) => (
    correction.max_score > 0 && effectiveCorrectionScore(correction) !== null
  )).length;
  const studentReturnParams = new URLSearchParams(searchParams);
  studentReturnParams.delete("student_q");
  const studentReturn = studentReturnParams.toString();

  return (
    <section className="rounded-[10px] border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to={listHref} className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-primary hover:underline"><ArrowLeft aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "返回学生分析总览", "Back to student overview")}</Link>
          <div className="mt-2 inline-flex max-w-full flex-wrap items-baseline gap-x-3 rounded-[10px] bg-primary/[0.07] px-4 py-2.5 ring-1 ring-inset ring-primary/10">
            <h2 className="max-w-full truncate text-[28px] font-extrabold tracking-[-0.025em] text-primary">{student.name}</h2>
            <span className="max-w-full truncate text-[16px] font-bold text-primary/75">{student.id}</span>
          </div>
          <p className="mt-1.5 text-[12px] text-muted-foreground">{tx(locale, "当前选中学生 · 连续题目视图", "Selected student · Continuous question view")}</p>
        </div>
        <StudentStepButtons locale={locale} previous={previousStudent} next={nextStudent} onSelect={goToStudent} />
      </div>

      <QuestionFilterBar
        className="mt-4"
        locale={locale}
        value={questionQuery}
        matches={questionMatches}
        onQuery={(value) => setFilter("question_q", value)}
        onSelect={scrollToQuestion}
      />

      <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-6">
        <DetailMetric label={tx(locale, "总分", "Total score")} value={`${formatScore(student.totalScore)} / ${formatScore(student.totalMax)}`} tone="primary" />
        <DetailMetric label={tx(locale, "得分率", "Score Percentage")} value={formatPercent(student.percent)} tone="accent" />
        <DetailMetric label={tx(locale, "平均置信度", "Mean confidence")} value={formatConfidence(student.avgConfidence)} tone="primary" />
        <DetailMetric label={tx(locale, "及格状态", "Pass status")} value={student.percent === null ? "—" : student.percent >= 60 ? tx(locale, "及格", "Passed") : tx(locale, "未及格", "Failed")} tone={student.percent !== null && student.percent < 60 ? "danger" : "accent"} />
        <DetailMetric label={tx(locale, "低置信题次", "Low-confidence items")} value={String(student.lowConfidenceCount)} tone="warning" />
        <DetailMetric label={tx(locale, "未人工处理提醒", "Unreviewed flags")} value={String(pendingReviewCount)} tone="warning" />
      </div>

      <div className="mt-5 grid items-start gap-4 lg:grid-cols-[132px_minmax(0,1fr)]">
        <ResultQuestionSidebar
          locale={locale}
          questions={visibleQuestions}
          activeId={activeQuestionId}
          onSelect={scrollToQuestion}
          stateForQuestion={(item) => questionStateForStudent(student, item.id)}
          behavior="locate"
        />
        <div className="min-w-0">
          <ContinuousQuestionsView locale={locale} taskId={taskId} student={student} questions={visibleQuestions} validCorrectionCount={validCorrectionCount} studentReturn={studentReturn} onSelect={scrollToQuestion} />
        </div>
      </div>
    </section>
  );
}

function QuestionFilterBar({ className, locale, value, matches, onQuery, onSelect }: {
  className?: string;
  locale: Locale;
  value: string;
  matches: ReviewSearchMatch[];
  onQuery: (value: string) => void;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const smartSearch = useImeSafeQuery({ value, onCommit: onQuery, onDraftChange: () => setOpen(true) });

  return (
    <section className={cn("relative rounded-[9px] border bg-background p-2.5", className)} aria-label={tx(locale, "题目筛选", "Question filter")}>
      <div>
        <label className="relative block min-w-0">
          <span className="sr-only">{tx(locale, "搜索题目", "Search questions")}</span>
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={smartSearch.draftValue}
            inputMode="search"
            onFocus={() => setOpen(true)}
            onCompositionStart={smartSearch.handleCompositionStart}
            onCompositionEnd={smartSearch.handleCompositionEnd}
            onChange={smartSearch.handleChange}
            onBlur={(event) => {
              smartSearch.handleBlur(event);
              window.setTimeout(() => setOpen(false), 120);
            }}
            placeholder={tx(locale, "SmarTAI 智能搜索：题号、题型、题干，或“积分题”", "SmarTAI Smart Search: number, type, stem, or “integration”")}
            className="h-10 w-full rounded-[8px] border bg-card pl-10 pr-10 text-xs text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
          />
          {smartSearch.draftValue ? <button type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => { smartSearch.commitValue(""); setOpen(false); }} aria-label={tx(locale, "清空题目筛选", "Clear question filter")} className="absolute right-2 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted"><X aria-hidden="true" className="h-3.5 w-3.5" /></button> : null}
          {open && smartSearch.draftValue.trim() && smartSearch.draftValue === value ? <SearchResults locale={locale} matches={matches} onSelect={(id) => { onSelect(id); setOpen(false); }} /> : null}
        </label>
      </div>
      <div className="mt-2 grid gap-0.5 px-1 text-[10px] leading-4 text-muted-foreground">
        <p>{tx(locale, "搜索只筛选题目，当前学生保持不变；输入中文时在选词完成后应用。", "Search filters questions only and keeps the current student; IME text is applied after composition.")}</p>
        <p>{tx(locale, "输入框聚焦时方向键只编辑文本；退出输入框后，←/→ 切换学生，↑/↓ 切换题目。", "Arrow keys edit text while an input is focused; after leaving it, ←/→ switch students and ↑/↓ switch questions.")}</p>
      </div>
    </section>
  );
}

function SearchResults({ locale, matches, onSelect }: { locale: Locale; matches: ReviewSearchMatch[]; onSelect: (id: string) => void }) {
  return <div className="absolute left-0 right-0 top-9 z-30 max-h-60 overflow-y-auto rounded-[8px] border bg-card p-1.5 shadow-lg">{matches.length ? matches.slice(0, 12).map((match) => <button key={match.item.id} type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => onSelect(match.item.id)} className="flex w-full items-center gap-3 rounded-[6px] px-2.5 py-2 text-left hover:bg-muted"><span className="min-w-0 flex-1"><span className="block truncate text-xs font-semibold text-foreground">{match.item.primary}</span><span className="block truncate text-[10px] text-muted-foreground">{match.item.secondary}</span></span><span className={cn("rounded-full px-2 py-1 text-[9px] font-semibold", match.kind === "exact" ? "bg-emerald-100 text-emerald-700" : "bg-blue-50 text-primary")}>{match.kind === "exact" ? tx(locale, "完全匹配", "Exact") : tx(locale, "相关匹配", "Related")}</span></button>) : <p className="px-3 py-4 text-center text-xs text-muted-foreground">{tx(locale, "没有匹配项，可清空当前筛选。", "No matches. Clear this filter to reset.")}</p>}</div>;
}

function ContinuousQuestionsView({ locale, taskId, student, questions, validCorrectionCount, studentReturn, onSelect }: { locale: Locale; taskId: string; student: StudentSummary; questions: QuestionSummary[]; validCorrectionCount: number; studentReturn: string; onSelect: (id: string) => void }) {
  return (
    <section aria-labelledby="student-all-questions-title">
      <div className="flex flex-wrap items-end justify-between gap-3"><div><h3 id="student-all-questions-title" className="text-[16px] font-bold text-foreground">{tx(locale, "全部题目", "All questions")}</h3><p className="mt-1 text-[11px] text-muted-foreground">{tx(locale, "连续展示每道题的作答与最终结果；侧栏、按钮和 ↑/↓ 键均定位到对应题目。", "Every response and final result stays in one continuous view; the sidebar, buttons, and ↑/↓ keys move between questions.")}</p></div><span className="text-[11px] text-muted-foreground">{tx(locale, `${questions.length} 道筛选结果 · ${validCorrectionCount} 道可计算得分率`, `${questions.length} filtered · ${validCorrectionCount} with comparable score percentages`)}</span></div>
      {questions.length ? <div className="mt-3 grid gap-3">{questions.map((question, index) => <QuestionResultCard key={question.id} locale={locale} taskId={taskId} student={student} question={question} studentReturn={studentReturn} previous={index > 0 ? questions[index - 1] : null} next={index < questions.length - 1 ? questions[index + 1] : null} onSelect={onSelect} />)}</div> : <PanelEmpty locale={locale} text={tx(locale, "当前题目筛选没有结果；清空题目筛选即可恢复。", "No questions match this filter; clear the question filter to restore all.")} />}
    </section>
  );
}

function QuestionResultCard({ locale, taskId, student, question, studentReturn, previous, next, onSelect }: { locale: Locale; taskId: string; student: StudentSummary; question: QuestionSummary; studentReturn: string; previous: QuestionSummary | null; next: QuestionSummary | null; onSelect: (id: string) => void }) {
  const correction = student.corrections.find((item) => item.q_id === question.id);
  const answer = student.answerByQuestion.get(question.id);
  if (!correction) return <article className="rounded-[9px] border px-4 py-5"><p className="text-[12px] text-muted-foreground">{question.label} · {tx(locale, "当前学生没有该题批改结果。", "No grading result is available for this student.")}</p></article>;
  const finalScore = effectiveCorrectionScore(correction);
  const percent = finalScore !== null && correction.max_score > 0
    ? (finalScore / correction.max_score) * 100
    : null;
  return (
    <article id={`question-${question.id}`} className="scroll-mt-24 rounded-[9px] border px-4 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><div className="flex flex-wrap items-center gap-2"><h4 className="text-[15px] font-bold text-foreground">{question.label}</h4><span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">{question.type || tx(locale, "未标题型", "Unlabeled type")}</span><ReviewStatus locale={locale} correction={correction} /></div><p className="mt-1 text-[11px] text-muted-foreground">{tx(locale, `最终 ${formatScore(finalScore)} / ${formatScore(correction.max_score)} · ${formatPercent(percent)} · 置信度 ${formatConfidence(correction.confidence)}`, `Final ${formatScore(finalScore)} / ${formatScore(correction.max_score)} · ${formatPercent(percent)} · confidence ${formatConfidence(correction.confidence)}`)}</p></div>
        <div className="flex flex-wrap items-center justify-end gap-2"><QuestionStepButtons locale={locale} previous={previous} next={next} onSelect={onSelect} /><Link to={questionDetailHref(taskId, student.id, question.id, studentReturn)} className="inline-flex h-8 items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-primary hover:bg-muted">{tx(locale, "题目分析", "Question analysis")}<ArrowRight aria-hidden="true" className="h-3.5 w-3.5" /></Link></div>
      </div>

      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <ContentPanel title={tx(locale, "学生作答", "Student Response")}><MarkdownMath className="text-[13px] leading-6 text-foreground">{answer?.content || tx(locale, "未识别到该题作答。", "No response was recognized for this question.")}</MarkdownMath></ContentPanel>
        <ContentPanel title={tx(locale, "SmarTAI 原始批改结果", "Original SmarTAI Result")} meta={`${formatScore(aiCorrectionScore(correction))} / ${formatScore(correction.max_score)}`}><MarkdownMath className="text-[12px] leading-5 text-muted-foreground">{correction.comment || tx(locale, "SmarTAI 未返回文字依据。", "SmarTAI returned no written rationale.")}</MarkdownMath><div className="mt-3 grid grid-cols-3 gap-2"><SmallSignal label={tx(locale, "置信度", "Confidence")} value={formatConfidence(correction.confidence)} /><SmallSignal label={tx(locale, "专家数", "Models")} value={String(correction.expert_results?.length ?? 0)} /><SmallSignal label={tx(locale, "合成方式", "Synthesis")} value={formatSynthesis(correction.synthesis_method, locale)} /></div></ContentPanel>
        <ContentPanel title={tx(locale, "最终采用结果", "Final Applied Result")} meta={`${formatScore(finalScore)} / ${formatScore(correction.max_score)}`}><p className="text-[12px] leading-5 text-foreground">{correction.teacher_comment?.trim() || tx(locale, "没有额外教师评语。", "No additional teacher comment.")}</p><p className="mt-3 text-[10px] font-semibold text-muted-foreground">{formatCorrectionScoreSource(correction, locale)}</p></ContentPanel>
        <ContentPanel title={tx(locale, "题干与评分标准", "Question & rubric")}><MarkdownMath className="text-[12px] leading-5 text-foreground">{question.stem || tx(locale, "未提供题干。", "No question stem was provided.")}</MarkdownMath><div className="my-3 border-t" /><MarkdownMath className="text-[12px] leading-5 text-muted-foreground">{question.criterion || tx(locale, "未提供评分标准。", "No rubric was provided.")}</MarkdownMath></ContentPanel>
      </div>
      <div className="mt-3 flex justify-end"><QuestionStepButtons locale={locale} previous={previous} next={next} onSelect={onSelect} /></div>
    </article>
  );
}

function ContentPanel({ title, meta, children }: { title: string; meta?: string; children: ReactNode }) {
  return <section className="min-w-0 rounded-[8px] bg-muted/45 px-4 py-3"><div className="flex items-center justify-between gap-3"><h5 className="text-[11px] font-bold text-foreground">{title}</h5>{meta ? <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-primary">{meta}</span> : null}</div><div className="mt-2">{children}</div></section>;
}

function DetailMetric({ label, value, tone }: { label: string; value: string; tone: "primary" | "accent" | "warning" | "danger" }) {
  return <div className="rounded-[9px] border px-3 py-3"><strong className={cn("text-[17px] leading-6", tone === "primary" && "text-primary", tone === "accent" && "text-teal-500", tone === "warning" && "text-amber-500", tone === "danger" && "text-rose-500")}>{value}</strong><span className="mt-1 block text-[10px] font-medium text-muted-foreground">{label}</span></div>;
}

function SmallSignal({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-[7px] bg-card px-2 py-2"><span className="block truncate text-[9px] text-muted-foreground">{label}</span><strong className="mt-0.5 block truncate text-[10px] text-foreground" title={value}>{value}</strong></div>;
}

function StudentStepButtons({ locale, previous, next, onSelect }: { locale: Locale; previous: StudentSummary | null; next: StudentSummary | null; onSelect: (id: string) => void }) {
  return <div className="flex items-center gap-2"><button type="button" disabled={!previous} onClick={() => previous && onSelect(previous.id)} className="inline-flex h-9 items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-foreground hover:bg-muted disabled:opacity-35"><ArrowLeft aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "上一位", "Previous")}</button><button type="button" disabled={!next} onClick={() => next && onSelect(next.id)} className="inline-flex h-9 items-center gap-1.5 rounded-[7px] border bg-card px-3 text-[11px] font-semibold text-foreground hover:bg-muted disabled:opacity-35">{tx(locale, "下一位", "Next")}<ArrowRight aria-hidden="true" className="h-3.5 w-3.5" /></button></div>;
}

function QuestionStepButtons({ locale, previous, next, onSelect }: { locale: Locale; previous: QuestionSummary | null; next: QuestionSummary | null; onSelect: (id: string) => void }) {
  return <div className="flex items-center gap-2"><button type="button" disabled={!previous} onClick={() => previous && onSelect(previous.id)} className="inline-flex h-8 items-center gap-1 rounded-[7px] border bg-card px-3 text-[10px] font-semibold text-foreground hover:bg-muted disabled:opacity-35"><ArrowUp aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "上一题", "Previous Q")}</button><button type="button" disabled={!next} onClick={() => next && onSelect(next.id)} className="inline-flex h-8 items-center gap-1 rounded-[7px] border bg-card px-3 text-[10px] font-semibold text-foreground hover:bg-muted disabled:opacity-35">{tx(locale, "下一题", "Next Q")}<ArrowDown aria-hidden="true" className="h-3.5 w-3.5" /></button></div>;
}

function ReviewStatus({ locale, correction }: { locale: Locale; correction: Correction }) {
  const source = correctionScoreSource(correction);
  const required = correctionNeedsFormalReview(correction);
  const label = source === "teacher_confirmed_same"
    ? tx(locale, "教师已处理 · 沿用 AI 分", "Teacher handled · AI score retained")
    : source === "teacher_changed"
      ? tx(locale, "教师改分", "Teacher changed")
      : source === "hard_failure"
        ? tx(locale, "无有效分数", "No valid score")
        : required
          ? tx(locale, "AI 分 · 未人工处理", "AI score · not reviewed")
          : tx(locale, "AI 自动评分", "AI scored");
  return <span className={cn("rounded-full px-2 py-0.5 text-[9px] font-semibold", source === "teacher_confirmed_same" && "bg-emerald-100 text-emerald-700", source === "teacher_changed" && "bg-blue-50 text-primary", source === "hard_failure" && "bg-rose-100 text-rose-700", source === "ai_untouched" && required && "bg-amber-100 text-amber-700", source === "ai_untouched" && !required && "bg-slate-100 text-slate-600")}>{label}</span>;
}

function DetailState({ locale, title, href }: { locale: Locale; title: string; href: string }) {
  return <section className="rounded-[10px] border bg-card px-6 py-12 text-center"><h2 className="text-[18px] font-bold text-foreground">{title}</h2><Link to={href} className="mt-5 inline-flex h-9 items-center gap-2 rounded-[8px] bg-primary px-4 text-[12px] font-semibold text-primary-foreground"><ArrowLeft aria-hidden="true" className="h-4 w-4" />{tx(locale, "返回学生总览", "Back to student overview")}</Link></section>;
}

function PanelEmpty({ locale, text }: { locale: Locale; text: string }) {
  return <div className="mt-3 rounded-[9px] border border-dashed px-5 py-10 text-center text-[12px] text-muted-foreground" aria-label={tx(locale, "空结果", "Empty result")}>{text}</div>;
}

function questionDetailHref(taskId: string, studentId: string, questionId: string, studentReturn: string): string {
  const params = new URLSearchParams({ student: studentId });
  if (studentReturn) params.set("student_return", studentReturn);
  return `/tasks/${encodeURIComponent(taskId)}/results/questions/${encodeURIComponent(questionId)}?${params.toString()}`;
}

function correctionNeedsFormalReview(correction: Correction): boolean {
  const confidence = normalizeConfidence(correction.confidence);
  return (confidence !== null && confidence < 0.65) || correction.requires_human_review || correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high") || correction.synthesis_method === "all_failed" || correction.synthesis_method === "quota_exhausted" || correctionHasDisagreement(correction);
}

function questionStateForStudent(student: StudentSummary, questionId: string): ResultQuestionState {
  const correction = student.corrections.find((item) => item.q_id === questionId);
  if (!correction) return "muted";
  const source = correctionScoreSource(correction);
  if (source === "teacher_confirmed_same" || source === "teacher_changed") return "ready";
  if (source === "hard_failure") return "danger";
  return correctionNeedsFormalReview(correction) ? "warning" : "ready";
}

function correctionHasDisagreement(correction: Correction): boolean {
  if (correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")) return true;
  const scores = correction.expert_results?.map((result) => Number(result.score)).filter(Number.isFinite) ?? [];
  return scores.length > 1 && correction.max_score > 0 && Math.max(...scores) - Math.min(...scores) > Math.max(1, correction.max_score * 0.25);
}

function formatSynthesis(value: string | null | undefined, locale: Locale): string {
  const labels: Record<string, [string, string]> = { single: ["单专家", "Single"], median: ["中位数", "Median"], multi_sample: ["多次采样", "Multi-sample"], weighted_average: ["加权平均", "Weighted"], judge_agent: ["裁判模型", "Judge"], degraded_to_single: ["降级单专家", "Fallback"], all_failed: ["全部失败", "Failed"], quota_exhausted: ["额度耗尽", "Quota"] };
  const pair = value ? labels[value] : null;
  return pair ? pair[locale === "en-US" ? 1 : 0] : value || "—";
}

function normalizeConfidence(value: number | null | undefined): number | null { if (typeof value !== "number" || !Number.isFinite(value)) return null; return value > 1 ? value / 100 : value; }
function isEditableTarget(target: EventTarget | null): boolean { return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)); }
function tx(locale: Locale, zh: string, en: string): string { return locale === "en-US" ? en : zh; }
