import { useMemo, useState } from "react";
import { TaskQueryBar } from "@/components/tasks/AskQueryBar";
import { effectiveCorrectionScore, type QuestionSummary } from "@/components/tasks/resultsModel";
import { useTaskFilterIntent, type TaskFilterController } from "@/hooks/useTaskFilterIntent";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { resolveResultQuestionQuery, selectResultQuestions } from "@/routes/tasks/results/QuestionAnalysisOverview";

/** Student detail must evaluate that student's corrections, never class averages. */
export function scopeResultQuestions(questions: QuestionSummary[], studentId?: string): QuestionSummary[] {
  if (studentId === undefined) return questions;
  return questions.map((question) => {
    const entries = question.entries.filter((entry) => entry.student.id === studentId);
    const correction = entries[0]?.correction;
    const score = correction ? effectiveCorrectionScore(correction) : null;
    const maximum = correction?.max_score ?? question.maxScore;
    const confidence = correction && Number.isFinite(correction.confidence) ? (correction.confidence > 1 ? correction.confidence / 100 : correction.confidence) : null;
    return { ...question, entries, count: entries.length, avgScore: score, minScore: score, maxObservedScore: score,
      maxScore: maximum, avgPercent: score !== null && maximum > 0 ? score / maximum * 100 : null,
      lowConfidenceCount: confidence !== null && confidence < 0.65 ? 1 : 0,
      reviewCount: correction?.requires_human_review ? 1 : 0 };
  });
}

export function useResultQuestionFilter({ taskId, questions, locale, studentId, queryParam = "question_q" }: {
  taskId?: string; questions: QuestionSummary[]; locale: Locale; studentId?: string; queryParam?: string;
}) {
  const scoped = useMemo(() => scopeResultQuestions(questions, studentId), [questions, studentId]);
  const filter = useTaskFilterIntent({ taskId, surface: "question_analysis", queryParam, contextKey: studentId,
    resolveLocal: (query) => resolveResultQuestionQuery(scoped, query, locale) });
  const visibleQuestions = useMemo(() => selectResultQuestions(scoped, filter.intent, locale), [filter.intent, locale, scoped]);
  return { filter, visibleQuestions };
}

export function ResultQuestionQuery({ filter, questions, onSelect, taskId, locale, className }: {
  filter: TaskFilterController; questions: QuestionSummary[]; onSelect: (id: string) => void;
  taskId?: string; locale: Locale; className?: string;
}) {
  const [open, setOpen] = useState(false);
  const zh = locale === "zh-CN";
  return <section className={cn("relative min-w-0", className)}
    onFocusCapture={() => setOpen(true)} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false); }}>
    <TaskQueryBar filter={filter} taskId={taskId} locale={locale}
      label={zh ? "Ask SmarTAI：筛选题目" : "Ask SmarTAI: filter questions"}
      placeholder={zh ? "找出低置信题目，或按得分率排序" : "Find low-confidence questions, or sort by score percentage"} />
    {open && filter.query.trim() && filter.intent && !filter.pending ? <div className="absolute left-0 right-0 top-full z-40 max-h-64 overflow-y-auto rounded-lg border bg-card p-1.5 shadow-xl">
      {questions.length ? questions.slice(0, 40).map((question) => <button key={question.id} type="button"
        onMouseDown={(event) => event.preventDefault()} onClick={() => { onSelect(question.id); setOpen(false); }}
        className="flex min-h-10 w-full items-center gap-3 rounded-md px-3 py-2 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring">
        <span className="min-w-12 text-xs font-semibold">{question.label}</span>
        <span className="truncate text-xs text-muted-foreground">{question.type || question.stem || "—"}</span>
      </button>) : <p className="px-3 py-5 text-center text-xs text-muted-foreground">{zh ? "没有匹配题目；清空后可恢复全部。" : "No matching questions; clear the query to restore all."}</p>}
    </div> : null}
  </section>;
}
