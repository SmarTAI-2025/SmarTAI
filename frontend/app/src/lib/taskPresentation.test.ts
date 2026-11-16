import { describe, expect, it } from "vitest";
import type { TaskLite } from "@/types";
import { selectTaskSnapshot } from "./taskPresentation";

const task = (changes: Partial<TaskLite> = {}): TaskLite => ({
  task_id: "task", name: "Synthetic task", owner_id: "teacher", workflow_revision: 3,
  status: "grading", progress_percent: 44, eta_seconds: 900, updated_at: 20,
  created_at: 1, problem_count: 7, student_count: 31, kb_docs: {}, kb_doc_count: 0,
  ...changes,
});

describe("dashboard snapshot selection", () => {
  it.each(["grading", "grading_paused"] as const)("keeps a completed list over an equal-revision %s cache, even when its timestamp is later", status => {
    const listed = task({ status: "graded", progress_percent: 100, eta_seconds: 0 });
    expect(selectTaskSnapshot(listed, task({ status, updated_at: 30 }))).toBe(listed);
  });

  it("uses completion from the state poll even if the equal-revision list timestamp is later", () => {
    const selected = selectTaskSnapshot(task({ updated_at: 30 }), task({ status: "graded", progress_percent: 100, eta_seconds: 0 }));
    expect(selected).toMatchObject({ status: "graded", progress_percent: 100, eta_seconds: 0 });
  });

  it("does not regress finalized to graded within a revision", () => {
    const listed = task({ status: "finalized", progress_percent: 100 });
    expect(selectTaskSnapshot(listed, task({ status: "graded", updated_at: 30 }))).toBe(listed);
    expect(selectTaskSnapshot(task({ status: "graded", updated_at: 30 }), listed).status).toBe("finalized");
  });

  it("prefers a newer workflow revision over completion from the previous run", () => {
    const completed = task({ status: "graded", progress_percent: 100, eta_seconds: 0 });
    const restarted = task({ workflow_revision: 4, progress_percent: 5 });
    expect(selectTaskSnapshot(completed, restarted)).toMatchObject({ status: "grading", progress_percent: 5 });
    expect(selectTaskSnapshot(restarted, completed)).toBe(restarted);
  });

  it("uses updated_at for equal-revision nonterminal snapshots", () => {
    const paused = task({ status: "grading_paused", progress_percent: 60, eta_seconds: null, updated_at: 30 });
    const running = task();
    expect(selectTaskSnapshot(paused, running)).toBe(paused);
    expect(selectTaskSnapshot(running, paused)).toMatchObject({ status: "grading_paused", progress_percent: 60, eta_seconds: null });
  });

  it("keeps live progress updates with identical revision, stage and timestamp", () => {
    expect(selectTaskSnapshot(task(), task({ progress_percent: 55 }))).toMatchObject({ progress_percent: 55 });
  });
});
