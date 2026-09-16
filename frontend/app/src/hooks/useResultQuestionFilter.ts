import { useMemo } from "react";
import { effectiveCorrectionScore, type QuestionSummary } from "@/components/tasks/resultsModel";
import { useTaskFilterIntent } from "@/hooks/useTaskFilterIntent";
import type { Locale } from "@/i18n/messages";
import { questionSearchItems, type ReviewSearchMatch } from "@/lib/reviewDetail";
import { localQuestionIntent, questionIntentSupported, selectResultQuestions } from "@/routes/tasks/results/QuestionAnalysisOverview";

export function useResultQuestionFilter({ taskId, query, questions, locale, studentId }: {
  taskId?: string; query: string; questions: QuestionSummary[]; locale: Locale; studentId?: string;
}) {
  const scopedQuestions = useMemo(() => studentId ? questions.map((question) => {
    const entries = question.entries.filter((entry) => entry.student.id === studentId);
    const correction = entries[0]?.correction;
    const score = correction ? effectiveCorrectionScore(correction) : null;
    const maximum = correction?.max_score ?? question.maxScore;
    return { ...question, entries, count: entries.length, avgScore: score, minScore: score, maxObservedScore: score,
      maxScore: maximum, avgPercent: score !== null && maximum > 0 ? score / maximum * 100 : null,
      lowConfidenceCount: correction && Number.isFinite(correction.confidence) && correction.confidence < 0.65 ? 1 : 0,
      reviewCount: correction?.requires_human_review ? 1 : 0 };
  }) : questions, [questions, studentId]);
  const localIntent = useMemo(() => localQuestionIntent(scopedQuestions, query, locale), [locale, query, scopedQuestions]);
  const controller = useTaskFilterIntent({
    taskId,
    query,
    surface: "question_analysis",
    localIntent,
    resolveLocalIntent: (value) => localQuestionIntent(scopedQuestions, value, locale),
    contextKey: studentId,
  });
  const unsupported = controller.intent && !questionIntentSupported(controller.intent);
  const filter = unsupported ? { ...controller, intent: null, unrecognized: true, explanation: locale === "zh-CN" ? "此条件超出当前题目列表支持的范围；未应用部分筛选。" : "This condition is not supported by this question list. No partial filter was applied." } : controller;
  const visibleQuestions = useMemo(() => selectResultQuestions(scopedQuestions, filter.intent, locale), [filter.intent, locale, scopedQuestions]);
  const matches: ReviewSearchMatch[] = useMemo(() => questionSearchItems(visibleQuestions).map((item) => ({ item, kind: item.exactValues.some((value) => value.toLowerCase() === query.trim().toLowerCase()) ? "exact" : "related" })), [query, visibleQuestions]);
  return { filter, visibleQuestions, matches };
}
