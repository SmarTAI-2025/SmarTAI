import { describe, expect, it } from "vitest";
import type { Correction } from "@/types";
import { buildResultsModel } from "./resultsModel";
import { collectResultReviewItems, correctionReviewAttention, correctionReviewBlocked, isCorrectionReviewConfirmed } from "./resultsReviewModel";

const correction = (overrides: Partial<Correction> = {}): Correction => ({
  q_id: "q1", type: "short", score: 8, max_score: 10, confidence: 0.9,
  comment: "", steps: [], expert_results: [], requires_human_review: false, review_reasons: [], ...overrides,
});

describe("grading review attention", () => {
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

  it("treats very low confidence and real expert disagreement as optional review", () => {
    expect(correctionReviewAttention(correction({ confidence: 0.2 }))).toBe("advisory");
    expect(correctionReviewBlocked(correction({ confidence: 0.2 }))).toBe(false);
    expect(correctionReviewAttention(correction({ review_reasons: ["score_spread_high"] }))).toBe("advisory");
    expect(correctionReviewAttention(correction({ confidence: 0.2, teacher_score: 8, review_status: "confirmed" }))).toBe("none");
  });

  it("orders blocking scores before optional checks without changing result calculations", () => {
    const corrections = [correction({ q_id: "normal", confidence: 0.6 }), correction({ q_id: "optional", confidence: 0.2 }), correction({ q_id: "missing", score: null, provisional_score: null })];
    const model = buildResultsModel(undefined, { task_id: "T1", status: "graded", timestamp: 0, results: [{ student_id: "s1", student_name: "Sample", corrections }] } as Parameters<typeof buildResultsModel>[1]);
    const before = JSON.stringify(corrections);
    const items = collectResultReviewItems(model, model.students);
    expect(items.map(item => [item.correction.q_id, item.severity])).toEqual([["missing", "blocking"], ["optional", "advisory"]]);
    expect(model.students[0].totalScore).toBe(16);
    expect(model.students[0].totalMax).toBe(20);
    expect(JSON.stringify(corrections)).toBe(before);
  });
});
