import { describe, expect, it } from "vitest";
import type { JobProgress } from "@/types";
import { calculateProgressPercent } from "./useTaskProgress";

function progress(overrides: Partial<JobProgress>): JobProgress {
  return {
    phase: "parsing",
    total_students: 0,
    total_questions: 0,
    completed_units: 0,
    active: [],
    messages: [],
    ...overrides,
  };
}

describe("question generation progress", () => {
  it("counts initial model work and finalization separately without declaring unfinished grading done", () => {
    const model = { id: "model-1", label: "Model", total: 217, succeeded: 80, failed: 0,
      cancelled: 0, processing: 0, queued: 0, responding: 0, retrying: 0, completion_times: [] };
    const value = progress({ phase: "grading", total_students: 31, total_questions: 7, completed_units: 4,
      model_progress: [model, { ...model, id: "model-2", succeeded: 199, failed: 1 }] });
    expect(calculateProgressPercent(value, "grading")).toBe(44);
    expect(calculateProgressPercent({ ...value, completed_units: 5 }, "grading")).toBeGreaterThanOrEqual(44);
    const finished = { ...value, completed_units: 217, model_progress: [{ ...model, succeeded: 217 }] };
    expect(calculateProgressPercent(finished, "grading")).toBe(99);
    expect(calculateProgressPercent({ ...finished, phase: "done" }, "graded")).toBe(100);
  });
  it("does not move backwards when generation metrics first appear", () => {
    expect(calculateProgressPercent(progress({
      current_step: "generating_solutions",
      completed_steps: 3,
      total_steps: 8,
      stage_metrics: {
        solution_total_questions: 7,
        solution_completed_questions: 0,
        solution_failed_questions: 0,
      },
    }))).toBe(38);
  });

  it("advances within the generation stage for each completed major question", () => {
    expect(calculateProgressPercent(progress({
      current_step: "generating_solutions",
      completed_steps: 3,
      total_steps: 8,
      stage_metrics: {
        solution_total_questions: 7,
        solution_completed_questions: 1,
        solution_failed_questions: 0,
      },
    }))).toBe(39);

    expect(calculateProgressPercent(progress({
      current_step: "generating_solutions",
      completed_steps: 3,
      total_steps: 8,
      stage_metrics: {
        solution_total_questions: 7,
        solution_completed_questions: 7,
        solution_failed_questions: 0,
      },
    }))).toBe(50);
  });

  it("does not count a failed major question as completed", () => {
    expect(calculateProgressPercent(progress({
      current_step: "generating_solutions",
      completed_steps: 3,
      total_steps: 8,
      stage_metrics: {
        solution_total_questions: 7,
        solution_completed_questions: 1,
        solution_failed_questions: 1,
      },
    }))).toBe(39);
  });

  it("keeps the legacy stage-only calculation when new metrics are absent", () => {
    expect(calculateProgressPercent(progress({
      current_step: "generating_solutions",
      completed_steps: 3,
      total_steps: 8,
    }))).toBe(38);
  });
});
