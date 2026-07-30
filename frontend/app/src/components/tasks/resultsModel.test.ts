import { describe, expect, it } from "vitest";
import type { Correction } from "@/types";
import {
  correctionReviewDraftScore,
  correctionReviewReasonIds,
  displayableCorrectionScore,
} from "./resultsModel";

function correction(patch: Partial<Correction> = {}): Correction {
  return {
    q_id: "q1",
    type: "short_answer",
    score: 0,
    max_score: 10,
    confidence: 0.4,
    comment: "The result needs review.",
    steps: [],
    expert_results: [],
    requires_human_review: true,
    review_reasons: ["low_confidence,high_indecisiveness"],
    review_status: "pending",
    ...patch,
  };
}

describe("review score presentation", () => {
  it("does not present the facade's fallback zero as a real pending grade", () => {
    const pending = correction();

    expect(displayableCorrectionScore(pending)).toBeNull();
    expect(correctionReviewDraftScore(pending)).toBe("");
  });

  it("shows a teacher-entered zero and a confirmed AI score", () => {
    expect(displayableCorrectionScore(correction({ teacher_score: 0, review_status: "edited" }))).toBe(0);
    expect(displayableCorrectionScore(correction({ score: 7, requires_human_review: false, review_status: "confirmed" }))).toBe(7);
  });

  it("normalizes comma-joined backend review reasons", () => {
    expect(correctionReviewReasonIds(correction())).toEqual(["low_confidence", "high_indecisiveness"]);
  });
});
