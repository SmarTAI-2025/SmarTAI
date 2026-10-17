import type { Correction } from "@/types";
import type { Locale } from "@/i18n/messages";
import { confidenceRequiresReview, confidenceValue } from "@/lib/confidencePolicy";
import {
  correctionReviewAttention,
  correctionReviewBlocked,
  correctionReviewReasonIds,
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

export { isCorrectionReviewConfirmed, correctionReviewBlocked, correctionReviewNextBlocked, correctionReviewAttention, getExpertScoreSpread } from "./resultsModel";

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
  const missingScore = correctionReviewBlocked(correction);
  const lowConfidence = confidenceRequiresReview(correction.confidence);
  const expertDisagreement = hasExpertDisagreement(correction);
  const confidence = confidenceValue(correction.confidence);
  const category = missingScore ? "review" : lowConfidence
    ? "low-confidence"
    : expertDisagreement
      ? "expert-disagreement"
      : "review";
  const priority =
    (missingScore ? 100 : 0) +
    (lowConfidence ? 40 : 0) +
    (expertDisagreement ? 20 : 0) +
    (confidence === null ? 0 : 1 - confidence);

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
  if (confidenceRequiresReview(correction.confidence)) {
    labels.add(text("置信度很低，请核对并确认", "Very low confidence; check and confirm the result"));
  }
  for (const reason of correctionReviewReasonIds(correction)) {
    if (["low_confidence", "degraded_to_single", "high_indecisiveness", "score_spread_high", "minority_veto"].includes(reason)) continue;
    labels.add(reviewReasonLabel(reason, locale));
  }
  if (hasExpertDisagreement(correction)) {
    labels.add(text("专家分歧较大（标准差 ÷ 满分 > 15%），可按需复核", "High expert disagreement (standard deviation / max score > 15%); optional review"));
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
