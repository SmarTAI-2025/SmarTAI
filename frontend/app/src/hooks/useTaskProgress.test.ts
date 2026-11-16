import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTaskProgress } from "./useTaskProgress";

const state = vi.hoisted(() => ({ data: {} as Record<string, unknown> }));
vi.mock("@/api/hooks/tasks", () => ({ useTaskState: () => ({ data: state.data }) }));

beforeEach(() => {
  state.data = { status: "grading", progress: { phase: "grading", messages: [],
    total_students: 31, total_questions: 7, completed_units: 216 } };
});

describe("one server progress projection for task pages and dashboard", () => {
  it("uses the server percentage instead of rounding counters to 100%", () => {
    state.data.progress_percent = 99;
    const { result, rerender } = renderHook(() => useTaskProgress("task"));
    expect(result.current.percent).toBe(99);
    state.data = { ...state.data, status: "graded", progress_percent: 100, progress: null };
    rerender();
    expect(result.current.percent).toBe(100);
  });

  it("does not replace whole-pipeline progress with a local unit calculation", () => {
    state.data.progress_percent = 44;
    expect(renderHook(() => useTaskProgress("task")).result.current.percent).toBe(44);
  });

  it.each([null, undefined, NaN])("keeps unavailable progress unknown (%s)", value => {
    state.data.progress_percent = value;
    expect(renderHook(() => useTaskProgress("task")).result.current.percent).toBeNull();
  });
});
