import type { Correction } from "@/types";
import {
  correctionReviewReasonIds,
  displayableCorrectionScore,
  hasReviewSignal,
  reviewReasonLabel,
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "./resultsModel";

export interface ReviewItem {
  student: StudentSummary;
  question: QuestionSummary;
  correction: Correction;
  priority: number;
  reasons: string[];
  category: "low-confidence" | "expert-disagreement" | "score-anomaly" | "review";
}

export function collectResultReviewItems(model: ResultsModel, students: StudentSummary[]): ReviewItem[] {
  return students
    .flatMap((student) =>
      student.corrections
        .map((correction) => buildReviewItem(student, correction, model.questions))
        .filter((item): item is ReviewItem => Boolean(item)),
    )
    .sort((a, b) => b.priority - a.priority);
}

export function getExpertScoreSpread(correction: Correction) {
  const scores = correction.expert_results
    ?.map((expert) => expert.score)
    .filter((score): score is number => typeof score === "number" && Number.isFinite(score));
  if (!scores?.length) {
    return 0;
  }
  return Math.max(...scores) - Math.min(...scores);
}

function buildReviewItem(
  student: StudentSummary,
  correction: Correction,
  questions: QuestionSummary[],
): ReviewItem | null {
  const question = questions.find((item) => item.id === correction.q_id) ?? fallbackQuestion(correction.q_id);
  const reasons = collectReasonLabels(correction);
  const expertSpread = getExpertScoreSpread(correction);
  const displayScore = displayableCorrectionScore(correction);
  const percent = displayScore !== null && correction.max_score > 0
    ? (displayScore / correction.max_score) * 100
    : null;
  const lowConfidence = correction.confidence < 0.65;
  const expertDisagreement =
    correctionReviewReasonIds(correction).some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high") ||
    expertSpread > Math.max(1, correction.max_score * 0.25);
  const scoreAnomaly = percent !== null && (percent <= 40 || percent >= 95) && hasReviewSignal(correction);

  if (!lowConfidence && !expertDisagreement && !scoreAnomaly && !correction.requires_human_review) {
    return null;
  }

  const category = lowConfidence
    ? "low-confidence"
    : expertDisagreement
      ? "expert-disagreement"
      : scoreAnomaly
        ? "score-anomaly"
        : "review";
  const priority =
    (lowConfidence ? 30 : 0) +
    (expertDisagreement ? 20 : 0) +
    (scoreAnomaly ? 10 : 0) +
    (correction.requires_human_review ? 5 : 0) +
    (1 - Math.min(1, Math.max(0, correction.confidence)));

  return {
    student,
    question,
    correction,
    priority,
    reasons,
    category,
  };
}

function collectReasonLabels(correction: Correction) {
  const labels = new Set<string>();
  if (correction.confidence < 0.65) {
    labels.add("置信度偏低");
  }
  for (const reason of correctionReviewReasonIds(correction)) {
    labels.add(reviewReasonLabel(reason));
  }
  if (getExpertScoreSpread(correction) > Math.max(1, correction.max_score * 0.25)) {
    labels.add("专家分数差异较大");
  }
  if (!labels.size && correction.requires_human_review) {
    labels.add("后端标记需复核");
  }
  return Array.from(labels);
}

function fallbackQuestion(qId: string): QuestionSummary {
  return {
    id: qId,
    label: qId,
    entries: [],
    count: 0,
    avgScore: 0,
    maxScore: 0,
    avgPercent: null,
    minScore: null,
    maxObservedScore: null,
    lowConfidenceCount: 0,
    reviewCount: 0,
  };
}
