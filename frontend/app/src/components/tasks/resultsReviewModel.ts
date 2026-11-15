import type { Correction } from "@/types";
import type { Locale } from "@/i18n/messages";
import { confidenceNeedsAttention, confidenceValue } from "@/lib/confidencePolicy";
import {
  correctionReviewAttention,
  correctionReviewBlocked,
  correctionReviewReasonIds,
  hasExpertDisagreement,
  hasPartialExperts,
  pendingProgrammingReview,
  validExpertResults,
  isLowConfidence,
  reviewReasonLabel,
  mathReviewLabels,
  MATH_REVIEW_REASONS,
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
    (attention === "blocking" ? 100 : 0) +
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
    add(text("缺少有效分数，请填写", "Enter a valid score"), "blocking");
  }
  const programming = pendingProgrammingReview(correction);
  if (programming) add(reviewReasonLabel(programming, locale), "blocking");
  if (correctionReviewReasonIds(correction).includes("programming_extra_boundary")) add(reviewReasonLabel("programming_extra_boundary", locale));
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
  const failedExperts = (correction.expert_results ?? []).filter(expert => expert.error_kind);
  const failureLabels = [...new Set(failedExperts.map(expert => `${expert.provider}：${reviewReasonLabel(expert.error_kind!, locale)}`))];
  if (hasPartialExperts(correction)) {
    add(text("部分专家完成，已保留有效评分和评语，建议核对。", "Some experts completed. Valid scores and feedback are retained; review suggested.")
      + (failureLabels.length ? ` ${failureLabels.join("；")}` : ""));
  } else for (const expert of failedExperts) {
    add(`${expert.provider}：${reviewReasonLabel(expert.error_kind!, locale)}`,
      correctionReviewBlocked(correction) ? "blocking" : "advisory");
  }
  for (const label of mathReviewLabels(correction, locale)) {
    add(label);
  }
  if (correctionReviewBlocked(correction)) for (const reason of correctionReviewReasonIds(correction)) {
    if ([...MATH_REVIEW_REASONS, "low_confidence", "partial_experts", "degraded_to_single", "high_indecisiveness", "score_spread_high", "minority_veto"].includes(reason)) continue;
    if (["all_failed", "missing_correction", "llm_failed"].includes(reason)
      || failedExperts.some(expert => expert.error_kind === reason)) continue;
    add(reviewReasonLabel(reason, locale), "blocking");
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
