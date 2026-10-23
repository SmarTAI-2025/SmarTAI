import type { Correction } from "@/types";
import type { Locale } from "@/i18n/messages";
import { confidenceNeedsAttention, confidenceValue } from "@/lib/confidencePolicy";
import {
  correctionReviewAttention,
  correctionReviewBlocked,
  correctionReviewReasonIds,
  hasExpertDisagreement,
  hasPartialExperts,
  validExpertResults,
  isLowConfidence,
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
  const lowConfidence = isLowConfidence(correction);
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
  return correctionReviewNotes(correction, locale).map(issue => issue.label);
}

export function correctionReviewNotes(correction: Correction, locale: Locale) {
  const text = (zh: string, en: string) => locale === "en-US" ? en : zh;
  const labels = new Map<string, "blocking" | "advisory">();
  const add = (label: string, severity: "blocking" | "advisory" = "advisory") => labels.set(label, severity);
  if (correctionReviewBlocked(correction)) {
    const score = effectiveCorrectionScore(correction);
    const valid = score !== null && Number.isFinite(correction.max_score) && correction.max_score > 0 && score >= 0 && score <= correction.max_score;
    add(valid ? text("缺失分数已补齐，请确认", "Corrected score needs confirmation") : text("缺少有效分数，请填写并确认", "Enter and confirm a valid score"), "blocking");
  }
  const experts = validExpertResults(correction);
  const multi = new Set((correction.expert_results ?? []).map(expert => expert.provider_id || expert.provider)).size > 1 || hasPartialExperts(correction);
  if (experts.length) {
    for (const expert of experts.filter(expert => confidenceNeedsAttention(expert.confidence))) {
      add(multi
        ? text(`${expert.provider} 置信度 ${expert.confidence} 较低，建议核对确认。`, `${expert.provider}: low confidence (${expert.confidence}); review suggested.`)
        : text(`置信度 ${expert.confidence} 较低，建议核对确认。`, `Low confidence (${expert.confidence}); review suggested.`));
    }
  } else if (confidenceNeedsAttention(correction.confidence) && !correctionReviewBlocked(correction)) {
    add(text(`置信度 ${correction.confidence} 较低，建议核对确认。`, `Low confidence (${correction.confidence}); review suggested.`));
  }
  if (hasPartialExperts(correction)) add(text("部分专家完成，已保留有效评分和评语，建议核对。", "Some experts completed. Valid scores and feedback are retained; review suggested."));
  if (!correctionReviewBlocked(correction) && correctionReviewReasonIds(correction).includes("math_scoring_uncertainty")) {
    add(reviewReasonLabel("math_scoring_uncertainty", locale));
  }
  if (correctionReviewBlocked(correction)) for (const reason of correctionReviewReasonIds(correction)) {
    if (["low_confidence", "partial_experts", "degraded_to_single", "high_indecisiveness", "score_spread_high", "minority_veto"].includes(reason)) continue;
    add(reviewReasonLabel(reason, locale), "blocking");
  }
  if (hasExpertDisagreement(correction)) {
    add(text("专家分歧较大（标准差 ÷ 满分 > 15%），可按需复核", "High expert disagreement (standard deviation / max score > 15%); optional review"));
  }
  return Array.from(labels, ([label, severity]) => ({ label, severity }));
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
