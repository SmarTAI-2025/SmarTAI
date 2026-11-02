import { describe, expect, it } from "vitest";
import type { Correction } from "@/types";
import {
  aiCorrectionScore,
  buildResultsModel,
  correctionScoreSource,
  correctionReviewDraftScore,
  correctionReviewAttention,
  correctionReviewNextBlocked,
  correctionReviewReasonIds,
  effectiveCorrectionScore,
  formatCorrectionScoreSource,
  formatConfidence,
  reviewConfirmationScore,
  reviewReasonLabel,
  summarizeReviewScoreSources,
} from "./resultsModel";

function correction(patch: Partial<Correction> = {}): Correction {
  return {
    q_id: "q1",
    type: "concept",
    score: null,
    provisional_score: 7,
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
  it("shows a residual math concern as optional despite high confidence and clears it after confirmation", () => {
    const pending = correction({
      score: 8, provisional_score: 8, confidence: .95,
      expert_results: [{ provider: "math", score: 8, max_score: 10, confidence: .95, comment: "Check the omitted condition.", steps: [] }],
      review_reasons: ["math_scoring_uncertainty"],
    });
    expect(correctionReviewAttention(pending)).toBe("advisory");
    expect(correctionReviewNextBlocked(pending)).toBe(false);
    expect(reviewReasonLabel("math_scoring_uncertainty", "zh-CN")).toBe("评分依据仍有疑点，建议核对");
    expect(reviewReasonLabel("math_scoring_uncertainty", "en-US")).toBe("Scoring evidence needs checking");
    expect(correctionReviewAttention({
      ...pending, teacher_score: 8, review_status: "confirmed", review_reasons: [],
      initial_review_reasons: ["math_scoring_uncertainty"],
    })).toBe("none");
  });

  it("preserves unknown confidence in display and excludes it from averages", () => {
    const result = { task_id: "T", status: "graded", timestamp: 0, results: [{ student_id: "S", corrections: [
      correction({ q_id: "unknown", confidence: null, score: 7, review_reasons: [] }),
      correction({ q_id: "known", confidence: 0.6, score: 7, review_reasons: [] }),
    ] }] };
    const model = buildResultsModel(undefined, result as Parameters<typeof buildResultsModel>[1]);
    expect(model.students[0].avgConfidence).toBe(0.6);
    expect(model.reviewCount).toBe(0);
    expect(formatConfidence(null)).toBe("--");
    expect(formatConfidence(60)).toBe("--");
  });

  it("shows a soft-review AI score and uses the same one-decimal teacher default", () => {
    const pending = correction();

    expect(correctionReviewDraftScore(pending)).toBe("7");
    expect(aiCorrectionScore(pending)).toBe(7);
    expect(effectiveCorrectionScore(pending)).toBe(7);
    expect(reviewConfirmationScore(pending, "")).toBe(7);
    expect(correctionScoreSource(pending)).toBe("ai_untouched");
    expect(correctionReviewDraftScore(correction({ provisional_score: 8.53 }))).toBe("8.5");
    expect(reviewConfirmationScore(correction({ provisional_score: 8.53 }), "")).toBe(8.5);
    expect(aiCorrectionScore(correction({ provisional_score: 2.55 }))).toBe(2.6);
    expect(correctionReviewDraftScore(correction({ provisional_score: 2.55, max_score: 2.55 }))).toBe("2.55");
  });

  it("does not mistake an auto-confirmed payload for teacher review", () => {
    const autoConfirmed = correction({
      score: 7,
      teacher_score: null,
      review_status: "confirmed",
    });

    expect(correctionScoreSource(autoConfirmed)).toBe("ai_untouched");
    expect(correctionReviewDraftScore(autoConfirmed)).toBe("7");
  });

  it("distinguishes a same-score confirmation from a teacher override", () => {
    const confirmed = correction({ score: 6, provisional_score: 6, teacher_score: 6, review_status: "confirmed" });
    const changed = correction({ score: 8, provisional_score: 6, teacher_score: 8, review_status: "confirmed" });

    expect(effectiveCorrectionScore(confirmed)).toBe(6);
    expect(correctionScoreSource(confirmed)).toBe("teacher_confirmed_same");
    expect(effectiveCorrectionScore(changed)).toBe(8);
    expect(correctionScoreSource(changed)).toBe("teacher_changed");
    expect(aiCorrectionScore(changed)).toBe(6);
  });

  it.each(["zh-CN", "en-US"] as const)("removes AI/unhandled labels after same-score confirmation (%s)", (locale) => {
    expect(formatCorrectionScoreSource(correction({ teacher_score: 7, review_status: "confirmed" }), locale)).toBe(locale === "zh-CN" ? "教师已确认" : "Teacher confirmed");
  });

  it("keeps a soft-review zero but rejects a legacy hard-failure zero", () => {
    const realZero = correction({ score: 0, provisional_score: 0 });
    const hardFailure = correction({
      score: 0,
      provisional_score: 0,
      synthesis_method: "all_failed",
      review_reasons: ["llm_failed"],
    });

    expect(effectiveCorrectionScore(realZero)).toBe(0);
    expect(reviewConfirmationScore(realZero, "")).toBe(0);
    expect(effectiveCorrectionScore(hardFailure)).toBeNull();
    expect(reviewConfirmationScore(hardFailure, "")).toBeNull();
    expect(correctionScoreSource(hardFailure)).toBe("hard_failure");
    const repaired = { ...hardFailure, teacher_score: 0, review_status: "confirmed" as const };
    expect(aiCorrectionScore(repaired)).toBeNull();
    expect(effectiveCorrectionScore(repaired)).toBe(0);
    expect(correctionScoreSource(repaired)).toBe("teacher_changed");
  });

  it("normalizes comma-joined backend review reasons", () => {
    expect(correctionReviewReasonIds(correction())).toEqual(["low_confidence", "high_indecisiveness"]);
  });

  it("counts review sources without treating hard failures as untouched AI scores", () => {
    const summary = summarizeReviewScoreSources([
      correction(),
      correction({ teacher_score: 7, score: 7, review_status: "confirmed" }),
      correction({ teacher_score: 8, score: 8, provisional_score: 7, review_status: "edited" }),
      correction({ score: 0, provisional_score: 0, synthesis_method: "all_failed", review_reasons: ["llm_failed"] }),
    ]);

    expect(summary).toEqual({
      aiUntouched: 2,
      teacherConfirmedSame: 1,
      teacherChanged: 0,
      hardFailure: 1,
      total: 4,
    });
  });
});

describe("formal result statistics", () => {
  it("includes untouched soft-review scores and real zero while excluding only hard failures", () => {
    const model = buildResultsModel(undefined, {
      status: "completed",
      task_id: "task-1",
      problem_data: {
        q1: {
          q_id: "q1",
          number: "1",
          type: "concept",
          stem: "Question 1",
          criterion: "Rubric 1",
          max_score: 10,
        },
        q2: {
          q_id: "q2",
          number: "2",
          type: "concept",
          stem: "Question 2",
          criterion: "Rubric 2",
          max_score: 20,
        },
      },
      results: [
        {
          student_id: "S1",
          student_name: "Zero Student",
          corrections: [
            correction({
              q_id: "q1",
              score: 0,
              provisional_score: 0,
              confidence: 0.9,
              requires_human_review: false,
              review_reasons: [],
              review_status: "confirmed",
            }),
            correction({ q_id: "q2", score: null, provisional_score: 12, max_score: 20 }),
          ],
        },
        {
          student_id: "S2",
          student_name: "Five Student",
          corrections: [
            correction({
              q_id: "q1",
              score: 5,
              confidence: 0.9,
              requires_human_review: false,
              review_reasons: [],
              review_status: "confirmed",
            }),
            correction({
              q_id: "q2",
              score: 0,
              provisional_score: 0,
              max_score: 20,
              synthesis_method: "all_failed",
              review_reasons: ["llm_failed"],
            }),
          ],
        },
      ],
    });

    expect(model.students[0]).toMatchObject({ totalScore: 5, totalMax: 10, percent: 50 });
    expect(model.students[1]).toMatchObject({ totalScore: 12, totalMax: 30, percent: 40 });
    expect(model.classAverageScore).toBe(8.5);
    expect(model.classAverageMax).toBe(20);
    expect(model.classAveragePercent).toBe(45);
    expect(model.questions[0]).toMatchObject({ avgScore: 2.5, minScore: 0, maxObservedScore: 5 });
    expect(model.questions[1]).toMatchObject({ avgScore: 12, avgPercent: 60, minScore: 12, maxObservedScore: 12 });
  });

  it("returns null class averages when every score is unresolved", () => {
    const model = buildResultsModel(undefined, {
      status: "completed",
      task_id: "task-1",
      results: [{
        student_id: "S1",
        corrections: [correction({
          score: null,
          provisional_score: null,
          synthesis_method: "all_failed",
          review_reasons: ["llm_failed"],
        })],
      }],
    });

    expect(model.students[0]).toMatchObject({ totalScore: 0, totalMax: 0, percent: null });
    expect(model.classAverageScore).toBeNull();
    expect(model.classAverageMax).toBeNull();
    expect(model.classAveragePercent).toBeNull();
  });
});
