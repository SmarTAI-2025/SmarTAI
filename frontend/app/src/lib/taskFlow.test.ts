import { describe, expect, it } from "vitest";
import { getTaskReachableStep, hasTaskReachedStep } from "./taskFlow";

describe("getTaskReachableStep", () => {
  it("opens read-only analysis as soon as grading has completed", () => {
    expect(getTaskReachableStep({ status: "graded" })).toBe(7);
  });
});

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

  it("allows submission upload after question replacement without opening stale grading", () => {
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

    expect(getTaskReachableStep(task)).toBe(3);
    expect(hasTaskReachedStep(task, 3)).toBe(true);
    expect(hasTaskReachedStep(task, 5)).toBe(false);
  });
  it("keeps missing questions and active extraction before response upload", () => {
    expect(getTaskReachableStep({ status: "problems_ready", problem_data: {} })).toBe(2);
    expect(getTaskReachableStep({ status: "extracting_problems", problem_count: 2 })).toBe(1);
  });
});
