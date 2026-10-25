import { describe, expect, it } from "vitest";
import type { Correction } from "@/types";
import { buildResultsModel, hasReviewSignal, summarizeReviewScoreSources } from "./resultsModel";
import { collectResultReviewItems, correctionReviewNotes, correctionReviewAttention, correctionReviewBlocked, correctionReviewNextBlocked, isCorrectionReviewConfirmed } from "./resultsReviewModel";

const correction = (overrides: Partial<Correction> = {}): Correction => ({
  q_id: "q1", type: "concept", score: 8, max_score: 10, confidence: 0.9,
  comment: "", steps: [], expert_results: [], requires_human_review: false, review_reasons: [], ...overrides,
});

describe("grading review attention", () => {
  it("does not block a valid grade merely because the task paused", () => {
    const paused = correction({ review_reasons: ["grading_paused"], requires_human_review: true });
    expect(correctionReviewAttention(paused)).toBe("none");
    expect(correctionReviewBlocked(paused)).toBe(false);
    expect(correctionReviewAttention({ ...paused, teacher_score: 8, review_status: "confirmed" })).toBe("none");
  });
  it("keeps ordinary unconfirmed and moderately uncertain scores out of the risk queue", () => {
    for (const value of [correction(), correction({ confidence: 0.6, requires_human_review: true, review_reasons: ["low_confidence"] }), correction({ score: 1, requires_human_review: true })]) {
      expect(correctionReviewAttention(value)).toBe("none");
      expect(correctionReviewBlocked(value)).toBe(false);
      expect(isCorrectionReviewConfirmed(value)).toBe(false);
    }
  });

  it("keeps missing or invalid scores red until corrected and explicitly confirmed", () => {
    const missing = correction({ score: null, provisional_score: null, review_reasons: ["llm_failed"] });
    expect(correctionReviewAttention(missing)).toBe("blocking");
    expect(correctionReviewAttention({ ...missing, score: 6, teacher_score: 6, review_status: "edited" })).toBe("blocking");
    const confirmed = { ...missing, score: 6, teacher_score: 6, review_status: "confirmed" as const };
    expect(correctionReviewAttention(confirmed)).toBe("none");
    expect(isCorrectionReviewConfirmed(confirmed)).toBe(true);
    expect(correctionReviewAttention(correction({ score: 11 }))).toBe("blocking");
    expect(correctionReviewAttention(correction({ score: Number.NaN }))).toBe("blocking");
  });

  it("treats every low confidence as advisory without treating legacy flags alone as disagreement", () => {
    expect(correctionReviewAttention(correction({ confidence: 0.2 }))).toBe("advisory");
    expect(correctionReviewBlocked(correction({ confidence: 0.2 }))).toBe(false);
    expect(correctionReviewNextBlocked(correction({ confidence: 0.2 }))).toBe(false);
    expect(correctionReviewAttention(correction({ confidence: 0.25 }))).toBe("advisory");
    expect(correctionReviewNextBlocked(correction({ confidence: 0.25 }))).toBe(false);
    expect(correctionReviewAttention(correction({ review_reasons: ["score_spread_high"] }))).toBe("none");
    expect(correctionReviewAttention(correction({ confidence: 0.2, teacher_score: 8, review_status: "confirmed" }))).toBe("none");
  });

  it("does not mistake a failed expert placeholder for disagreement, but retains real zero scores", () => {
    const successful = { provider: "a", score: 9, max_score: 10, confidence: .9, comment: "", steps: [] };
    const zero = { ...successful, provider: "b", score: 0, confidence: 0 };
    expect(correctionReviewAttention(correction({ expert_results: [successful, { ...zero, error_kind: "parse_failed" }] }))).toBe("advisory");
    expect(correctionReviewAttention(correction({ expert_results: [successful, zero] }))).toBe("advisory");
  });

  it.each([2, 2.6, 3, 3.01, 4])("uses population standard deviation over 15% without blocking results (spread=%s)", spread => {
    const value = correction({ expert_results: [10, 10 - spread].map(score => ({ provider: "expert", score, max_score: 10, confidence: .95, comment: "", steps: [] })) });
    expect(correctionReviewAttention(value)).toBe(spread > 3 ? "advisory" : "none");
    expect(correctionReviewNextBlocked(value)).toBe(false);
    expect(correctionReviewAttention({ ...value, teacher_score: 8, review_status: "confirmed" })).toBe("none");
  });

  it("uses each valid expert's confidence and lists only low experts", () => {
    const expert = { provider: "high", score: 8, max_score: 10, confidence: .9, comment: "", steps: [] };
    const value = correction({ confidence: .95, expert_results: [expert, { ...expert, provider: "low", confidence: .45 }] });
    expect(correctionReviewAttention(value)).toBe("advisory");
    expect(correctionReviewNotes(value, "en-US")).toEqual([{ severity: "advisory", label: "low: low confidence (0.45); review suggested." }]);
    expect(correctionReviewNotes({ ...value, expert_results: [{ ...expert, confidence: .45 }] }, "en-US")[0].label).toBe("Low confidence (0.45); review suggested.");
  });

  it.each([0, 8])("retains valid partial scores (%s), excluding failed confidence placeholders", score => {
    const expert = { provider: "valid", score, max_score: 10, confidence: .9, comment: "Saved feedback", steps: [] };
    const value = correction({ score, provisional_score: score, confidence: 0, review_reasons: ["quota_exhausted", "partial_experts"],
      expert_results: [expert, { ...expert, provider: "stopped", score: 0, confidence: 0, error_kind: "operation_cancelled" }] });
    expect(correctionReviewAttention(value)).toBe("advisory");
    expect(correctionReviewNextBlocked(value)).toBe(false);
    const notes = correctionReviewNotes(value, "en-US");
    expect(notes).toHaveLength(1);
    expect(notes[0]).toMatchObject({ severity: "advisory", label: expect.stringContaining("Some experts completed") });
    expect(correctionReviewAttention({ ...value, teacher_score: score, review_status: "confirmed" })).toBe("none");
  });

  it.each(["zh-CN", "en-US"] as const)("explains math evidence concerns without blocking a valid score (%s)", locale => {
    const value = correction({ confidence: .95, review_reasons: ["math_scoring_uncertainty"] });
    expect(correctionReviewAttention(value)).toBe("advisory");
    expect(correctionReviewNextBlocked(value)).toBe(false);
    expect(correctionReviewNotes(value, locale)).toEqual([{
      severity: "advisory",
      label: locale === "zh-CN" ? "评分依据仍有疑点，建议核对" : "Scoring evidence needs checking",
    }]);
    expect(correctionReviewAttention({ ...value, teacher_score: value.score, review_status: "confirmed" })).toBe("none");
  });

  it("excludes optional confidence checks from required counts while retaining explicit confidence-query data", () => {
    const corrections = [correction({ q_id: "normal", confidence: 0.6 }), correction({ q_id: "optional", confidence: 0.4 }), correction({ q_id: "missing", score: null, provisional_score: null })];
    const model = buildResultsModel(undefined, { task_id: "T1", status: "graded", timestamp: 0, results: [{ student_id: "s1", student_name: "Sample", corrections }] } as Parameters<typeof buildResultsModel>[1]);
    const before = JSON.stringify(corrections);
    const items = collectResultReviewItems(model, model.students);
    expect(items.map(item => [item.correction.q_id, item.severity])).toEqual([["missing", "blocking"], ["optional", "advisory"]]);
    expect(model.lowConfidenceCount).toBe(1);
    expect(model.reviewCount).toBe(2);
    expect(model.students[0].totalScore).toBe(16);
    expect(model.students[0].totalMax).toBe(20);
    expect(JSON.stringify(corrections)).toBe(before);
  });

  it("keeps analysis counts equal to the pending queue before and after confirmation", () => {
    const corrections = [
      correction({ q_id: "normal", confidence: .6, requires_human_review: true, review_reasons: ["low_confidence"] }),
      correction({ q_id: "disputed", confidence: .2, review_reasons: ["high_indecisiveness"] }),
      correction({ q_id: "edited", confidence: .2, teacher_score: 9, review_status: "edited", review_reasons: ["score_spread_high"] }),
      correction({ q_id: "zero", teacher_score: 0, review_status: "confirmed", confidence: .2 }),
      correction({ q_id: "same", teacher_score: 8, review_status: "confirmed", initial_review_reasons: ["score_spread_high"] }),
    ];
    const result = { task_id: "T1", status: "graded", timestamp: 0, results: [{ student_id: "S1", corrections }] };
    const model = buildResultsModel(undefined, result as Parameters<typeof buildResultsModel>[1]);
    expect(collectResultReviewItems(model, model.students).map(item => item.correction.q_id).sort()).toEqual(["disputed", "edited"]);
    expect(corrections.filter(hasReviewSignal).map(item => item.q_id).sort()).toEqual(["disputed", "edited"]);
    expect(model.reviewCount).toBe(2);
    expect(model.students[0].reviewCount).toBe(2);
    expect(model.questions.reduce((sum, question) => sum + question.reviewCount, 0)).toBe(2);
    expect(model.lowConfidenceCount).toBe(2);
    expect(summarizeReviewScoreSources(corrections).aiUntouched).toBe(2);
  });
});
