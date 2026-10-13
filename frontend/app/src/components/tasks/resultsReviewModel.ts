import type { Correction } from "@/types";
import { VERY_LOW_REVIEW_CONFIDENCE, type ReviewAttention } from "@/lib/reviewConfirmation";
import {
  correctionReviewReasonIds,
  effectiveCorrectionScore,
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
  severity: "blocking" | "advisory";
}

export function isCorrectionReviewConfirmed(correction: Correction): boolean {
  return Number.isFinite(correction.teacher_score) && correction.review_status === "confirmed" && !correctionReviewBlocked(correction);
}

function validScore(value: number | null | undefined, maxScore: number): boolean {
  return typeof value === "number" && Number.isFinite(value) && Number.isFinite(maxScore) && maxScore > 0 && value >= 0 && value <= maxScore;
}

/** Display/navigation guard only; effective AI and teacher score calculation is unchanged. */
export function correctionReviewBlocked(correction: Correction): boolean {
  if (!validScore(effectiveCorrectionScore(correction), correction.max_score)) return true;
  const originalScore = correction.provisional_score === undefined ? correction.score : correction.provisional_score;
  return !validScore(originalScore, correction.max_score) && correction.review_status !== "confirmed";
}

export function correctionReviewAttention(correction: Correction): ReviewAttention {
  if (correctionReviewBlocked(correction)) return "blocking";
  if (isCorrectionReviewConfirmed(correction)) return "none";
  const veryLowConfidence = Number.isFinite(correction.confidence) && correction.confidence >= 0 && correction.confidence <= VERY_LOW_REVIEW_CONFIDENCE;
  const expertDisagreement = correctionReviewReasonIds(correction).some(reason => reason === "high_indecisiveness" || reason === "score_spread_high")
    || getExpertScoreSpread(correction) > Math.max(1, correction.max_score * 0.25);
  return veryLowConfidence || expertDisagreement ? "advisory" : "none";
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
  const attention = correctionReviewAttention(correction);
  if (attention === "none") return null;
  const reasons = collectReasonLabels(correction);
  const expertSpread = getExpertScoreSpread(correction);
  const missingScore = attention === "blocking";
  const lowConfidence = Number.isFinite(correction.confidence) && correction.confidence >= 0 && correction.confidence <= VERY_LOW_REVIEW_CONFIDENCE;
  const expertDisagreement =
    correctionReviewReasonIds(correction).some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high") ||
    expertSpread > Math.max(1, correction.max_score * 0.25);
  const category = missingScore ? "review" : lowConfidence
    ? "low-confidence"
    : expertDisagreement
      ? "expert-disagreement"
      : "review";
  const priority =
    (missingScore ? 100 : 0) +
    (lowConfidence ? 30 : 0) +
    (expertDisagreement ? 20 : 0) +
    (1 - Math.min(1, Math.max(0, Number.isFinite(correction.confidence) ? correction.confidence : 1)));

  return {
    student,
    question,
    correction,
    priority,
    reasons,
    category,
    severity: attention,
  };
}

function collectReasonLabels(correction: Correction) {
  const labels = new Set<string>();
  if (correctionReviewBlocked(correction)) {
    labels.add(validScore(effectiveCorrectionScore(correction), correction.max_score) ? "缺失分数已补齐，请确认" : "缺少有效分数，请填写并确认");
  }
  if (Number.isFinite(correction.confidence) && correction.confidence >= 0 && correction.confidence <= VERY_LOW_REVIEW_CONFIDENCE) {
    labels.add("置信度很低，建议核对");
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
    avgScore: null,
    maxScore: 0,
    avgPercent: null,
    minScore: null,
    maxObservedScore: null,
    lowConfidenceCount: 0,
    reviewCount: 0,
  };
}
