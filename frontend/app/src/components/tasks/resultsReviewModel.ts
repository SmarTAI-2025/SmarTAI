import type { Correction } from "@/types";
import type { Locale } from "@/i18n/messages";
import { confidenceNeedsAttention, confidenceValue } from "@/lib/confidencePolicy";
import {
  correctionReviewAttention,
  correctionReviewBlocked,
  correctionReviewReasonIds,
  hasExpertDisagreement,
  hasPartialExperts,
  hasUnknownConfidence,
  pendingScoreReview,
  programmingAdvisoryReasons,
  validExpertResults,
  isCorrectionReviewConfirmed,
  hasSavedCorrectionReview,
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

export function pausedGradingMessage(correction: Correction, locale: Locale): string | null {
  if (isCorrectionReviewConfirmed(correction) || !correctionReviewReasonIds(correction).includes("grading_paused")) return null;
  if (!validExpertResults(correction).length) return locale === "zh-CN"
    ? "已暂停，尚无有效模型评分。可继续运行或人工填写。"
    : "Paused with no valid model grades. Resume grading or enter a score manually.";
  if (hasPartialExperts(correction)) return locale === "zh-CN"
    ? "部分模型初评已保存。可查看已有评分，或继续运行。"
    : "Some model grades are saved. View the saved grades or resume grading.";
  return locale === "zh-CN"
    ? "初评已保存，待 AI 核验与汇总。可查看已有评分，或继续运行。"
    : "Initial grades are saved, awaiting AI verification and aggregation. View the saved grades or resume grading.";
}

export function collectResultReviewItems(model: ResultsModel, students: StudentSummary[], locale: Locale = "zh-CN"): ReviewItem[] {
  return students
    .flatMap((student) =>
      student.corrections
        .map((correction) => buildReviewItem(student, correction, model.questions, locale))
        .filter((item): item is ReviewItem => Boolean(item)),
    )
    .sort((a, b) => b.priority - a.priority);
}

/** Overview shows only large score disagreement; detailed evidence stays intact. */
export function collectOverviewReviewItems(model: ResultsModel, students: StudentSummary[], locale: Locale = "zh-CN"): ReviewItem[] {
  const overviewStudents = students.map(student => ({
    ...student,
    corrections: student.corrections.map(correction => hasExpertDisagreement(correction) ? correction : {
      ...correction,
      // A display-only copy: preserve every independent concern and never
      // change the saved reasons used by the question detail or grading.
      review_reasons: correctionReviewReasonIds(correction).filter(reason => reason !== "math_expert_disagreement"),
      initial_review_reasons: [],
    }),
  }));
  return collectResultReviewItems(model, overviewStudents, locale);
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

export function correctionReviewNotes(correction: Correction, locale: Locale, compact = false) {
  const text = (zh: string, en: string) => locale === "en-US" ? en : zh;
  const labels = new Map<string, "blocking" | "advisory">();
  const add = (label: string, severity: "blocking" | "advisory" = "advisory") => {
    if (severity === "blocking" || !hasSavedCorrectionReview(correction)) labels.set(label, severity);
  };
  if (correctionReviewBlocked(correction)) {
    add(compact ? text("缺少有效分数，请填写。", "Enter a valid score.")
      : text("缺少有效分数，请填写", "Enter a valid score"), "blocking");
  }
  const pendingScore = pendingScoreReview(correction);
  if (pendingScore) add(reviewReasonLabel(pendingScore, locale), "blocking");
  for (const reason of programmingAdvisoryReasons(correction)) add(reviewReasonLabel(reason, locale));
  const experts = validExpertResults(correction);
  const multi = new Set((correction.expert_results ?? []).map(expert => expert.provider_id || expert.provider)).size > 1 || hasPartialExperts(correction);
  if (experts.length) {
    for (const expert of experts.filter(expert => confidenceNeedsAttention(expert.confidence))) {
      add(multi
        ? text(`${expert.provider} 置信度较低（${expert.confidence}），建议核对。`, `${expert.provider}: low confidence (${expert.confidence}). Review suggested.`)
        : text(`置信度较低（${expert.confidence}），建议核对。`, `Low confidence (${expert.confidence}). Review suggested.`));
    }
  } else if (confidenceNeedsAttention(correction.confidence) && !correctionReviewBlocked(correction)) {
    add(text(`置信度较低（${correction.confidence}），建议核对。`, `Low confidence (${correction.confidence}). Review suggested.`));
  }
  if (hasUnknownConfidence(correction) && !correctionReviewBlocked(correction)) {
    const unknown = experts.filter(expert => confidenceValue(expert.confidence) === null);
    if (multi && unknown.length) for (const expert of unknown) {
      add(text(`${expert.provider} 置信度未提供，建议核对。`, `${expert.provider}: confidence unavailable. Review suggested.`));
    } else add(reviewReasonLabel("confidence_unavailable", locale));
  }
  const failedExperts = (correction.expert_results ?? []).filter(expert => expert.error_kind);
  const failureLabels = [...new Set(failedExperts.map(expert => `${expert.provider}${text("：", ": ")}${reviewReasonLabel(expert.error_kind!, locale)}`))];
  if (hasPartialExperts(correction)) {
    add((compact ? text("部分专家未完成，建议核对。", "Some experts did not finish. Review suggested.")
      : text("部分专家完成，已保留有效评分和评语，建议核对。", "Some experts completed. Valid scores and feedback are retained; review suggested."))
      + (failureLabels.length ? ` ${failureLabels.join(text("；", " · "))}` : ""));
  } else for (const expert of failedExperts) {
    add(`${expert.provider}${text("：", ": ")}${reviewReasonLabel(expert.error_kind!, locale)}`,
      correctionReviewBlocked(correction) ? "blocking" : "advisory");
  }
  for (const label of mathReviewLabels(correction, locale)) {
    add(label);
  }
  if (correctionReviewBlocked(correction)) for (const reason of correctionReviewReasonIds(correction)) {
    if ([...MATH_REVIEW_REASONS, "low_confidence", "confidence_unavailable", "partial_experts", "degraded_to_single", "high_indecisiveness", "score_spread_high", "minority_veto"].includes(reason)) continue;
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
