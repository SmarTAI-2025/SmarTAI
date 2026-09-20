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
