import { describe, expect, it } from "vitest";
import type { GradeResult } from "@/types/education";
import { summarizeReleasedResults } from "./StudentResultPage";

function result(patch: Partial<GradeResult>): GradeResult {
  return {
    id: "result",
    grading_run_id: "run",
    question_id: "question",
    q_id: "q1",
    student_id: "student",
    ai_score: null,
    ai_max_score: 10,
    ai_comment: "",
    result_status: "graded",
    requires_review: false,
    review_reason: null,
    score: null,
    teacher_comment: "",
    ...patch,
  };
}

describe("summarizeReleasedResults", () => {
  it("includes released soft-review scores and real zero but excludes hard failures", () => {
    expect(summarizeReleasedResults([
      result({ id: "soft", result_status: "needs_review", effective_score: 6, ai_score: 6 }),
      result({ id: "zero", q_id: "q2", result_status: "needs_review", effective_score: 0, ai_score: 0 }),
      result({ id: "graded", q_id: "q3", effective_score: 5, ai_score: 5 }),
      result({ id: "failed", q_id: "q4", result_status: "failed", effective_score: null, ai_score: 0 }),
    ])).toEqual({ total: 11, max: 30 });
  });
});
