import { describe, expect, it } from "vitest";
import { getTaskReachableStep, hasTaskReachedStep } from "./taskFlow";

describe("task workflow rewinds", () => {
  it("keeps a failed question restart at the question upload stage even with old downstream ids", () => {
    const task = {
      task_id: "task-1",
      status: "error" as const,
      grading_setup_configured: true,
      last_failed_job_id: "extract-new",
      extract_job_id: "extract-new",
      parse_job_id: "parse-old",
      grading_job_id: "grade-old",
      submission_file_name: "old.zip",
      student_count: 20,
    };

    expect(getTaskReachableStep(task)).toBe(1);
    expect(hasTaskReachedStep(task, 3)).toBe(false);
  });

  it("disables submission and grading steps after a successful question replacement", () => {
    const task = {
      task_id: "task-1",
      status: "problems_ready" as const,
      grading_setup_configured: false,
      problem_data: {
        q1: {
          q_id: "q1",
          number: "1",
          type: "short",
          stem: "Question",
          criterion: "Rubric",
          max_score: 10,
          review_status: "needs_review" as const,
        },
      },
    };

    expect(getTaskReachableStep(task)).toBe(2);
    expect(hasTaskReachedStep(task, 3)).toBe(false);
    expect(hasTaskReachedStep(task, 5)).toBe(false);
  });
});
