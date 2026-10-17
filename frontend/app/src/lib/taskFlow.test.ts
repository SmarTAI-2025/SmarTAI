import { describe, expect, it } from "vitest";
import { getTaskDestination, getTaskReachableStep, hasTaskReachedStep, isTaskPaused, isTaskProcessing } from "./taskFlow";
import { HISTORY_STAGE_KEYS, HISTORY_ACTION_KEYS, historyStatusTone } from "@/components/history/historyPresentation";
import { TASK_STAGE_KEYS, TASK_ACTION_KEYS, taskStatusTone } from "./taskPresentation";
import { parseHistorySearchParams } from "@/components/history/historyQuery";

describe("shared task stages", () => {
  it.each([false, true])("opens question overview regardless of saved configuration (%s)", (configured) => {
    expect(getTaskDestination({ task_id: "T", status: "problems_ready", grading_setup_configured: configured }))
      .toBe("/tasks/T/questions");
  });
  it.each([
    ["submissions_ready", "/tasks/T/submissions"],
    ["grading_setup", "/tasks/T/grading-setup"],
  ] as const)("opens the correct workspace for %s", (status, destination) => {
    expect(getTaskDestination({ task_id: "T", status })).toBe(destination);
    expect(getTaskReachableStep({ status })).toBe(5);
  });
  it("keeps incomplete recognition recoverable even with saved configuration", () => {
    const task = { task_id: "T", status: "grading_setup" as const, submission_source_summary: {
      uploaded: 2, parsed: 1, failed: 1, pending: 0, identity_needs_review: 0,
    } };
    expect(getTaskDestination(task)).toBe("/tasks/T/submissions/progress");
    expect(getTaskReachableStep(task)).toBe(3);
  });
  it("shares all stage labels, actions and colors, and excludes obsolete filters", () => {
    expect(HISTORY_STAGE_KEYS).toBe(TASK_STAGE_KEYS);
    expect(HISTORY_ACTION_KEYS).toBe(TASK_ACTION_KEYS);
    expect(historyStatusTone).toBe(taskStatusTone);
    expect(Object.keys(TASK_STAGE_KEYS)).toHaveLength(13);
    expect(parseHistorySearchParams(new URLSearchParams("status=review_confirmed,generating_analysis,grading_setup")).statuses)
      .toEqual(["grading_setup"]);
  });
  it.each([
    ["problems_paused", "problems", 1],
    ["submissions_paused", "submissions", 3],
    ["grading_paused", "grading", 5],
  ] as const)("routes %s to its resumable progress without treating it as active or failed", (status, stage, step) => {
    expect(isTaskPaused(status)).toBe(true);
    expect(isTaskProcessing(status)).toBe(false);
    expect(getTaskDestination({ task_id: "T", status })).toBe(`/tasks/T/${stage}/progress`);
    expect(getTaskReachableStep({ status })).toBe(step);
    expect(taskStatusTone(status)).not.toContain("red");
    expect(parseHistorySearchParams(new URLSearchParams(`status=${status}`)).statuses).toEqual([status]);
  });
});

describe("getTaskReachableStep", () => {
  it("keeps legacy partial submission batches out of review and grading", () => {
    expect(getTaskReachableStep({ status: "submissions_ready", submission_source_summary: {
      uploaded: 2, parsed: 1, failed: 1, pending: 0, identity_needs_review: 0,
    } })).toBe(3);
  });
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
