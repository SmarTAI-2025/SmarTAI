import type { Correction } from "@/types";
import type { Locale } from "@/i18n/messages";
import { VERY_LOW_REVIEW_CONFIDENCE } from "@/lib/reviewConfirmation";
import {
  correctionReviewAttention,
  correctionReviewBlocked,
  correctionReviewReasonIds,
  getExpertScoreSpread,
  hasExpertDisagreement,
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

export { isCorrectionReviewConfirmed, correctionReviewBlocked, correctionReviewAttention, getExpertScoreSpread } from "./resultsModel";

export function collectResultReviewItems(model: ResultsModel, students: StudentSummary[], locale: Locale = "zh-CN"): ReviewItem[] {
  return students
    .flatMap((student) =>
      student.corrections
        .map((correction) => buildReviewItem(student, correction, model.questions, locale))
        .filter((item): item is ReviewItem => Boolean(item)),
    )
    .sort((a, b) => b.priority - a.priority);
}

function buildReviewItem(
  student: StudentSummary,
  correction: Correction,
  questions: QuestionSummary[],
  locale: Locale,
): ReviewItem | null {
  const question = questions.find((item) => item.id === correction.q_id) ?? fallbackQuestion(correction.q_id);
  const attention = correctionReviewAttention(correction);
  if (attention === "none") return null;
  const reasons = correctionReviewLabels(correction, locale);
  const missingScore = attention === "blocking";
  const lowConfidence = Number.isFinite(correction.confidence) && correction.confidence >= 0 && correction.confidence <= VERY_LOW_REVIEW_CONFIDENCE;
  const expertDisagreement = hasExpertDisagreement(correction);
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

export function correctionReviewLabels(correction: Correction, locale: Locale) {
  const text = (zh: string, en: string) => locale === "en-US" ? en : zh;
  const labels = new Set<string>();
  if (correctionReviewBlocked(correction)) {
    const score = effectiveCorrectionScore(correction);
    const valid = score !== null && Number.isFinite(correction.max_score) && correction.max_score > 0 && score >= 0 && score <= correction.max_score;
    labels.add(valid ? text("缺失分数已补齐，请确认", "Corrected score needs confirmation") : text("缺少有效分数，请填写并确认", "Enter and confirm a valid score"));
  }
  if (Number.isFinite(correction.confidence) && correction.confidence >= 0 && correction.confidence <= VERY_LOW_REVIEW_CONFIDENCE) {
    labels.add(text("置信度很低，建议核对", "Very low confidence; check the result"));
  }
  for (const reason of correctionReviewReasonIds(correction)) {
    labels.add(reviewReasonLabel(reason, locale));
  }
  if (getExpertScoreSpread(correction) > Math.max(1, correction.max_score * 0.25)) {
    labels.add(text("专家分数差异较大", "Large difference between expert scores"));
  }
  if (!labels.size && correction.requires_human_review) {
    labels.add(text("后端标记需复核", "Flagged for review"));
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
