import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { FilterIntentResult } from "@/types";
import { useTaskFilterIntent } from "./useTaskFilterIntent";

const mocks = vi.hoisted(() => ({ interpretFilterIntent: vi.fn() }));

vi.mock("@/api/analytics", () => ({ interpretFilterIntent: mocks.interpretFilterIntent }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function Harness() {
  const filter = useTaskFilterIntent({
    taskId: "task-1",
    query: "show the unusual records",
    surface: "question_preparation",
    localIntent: null,
  });
  return (
    <>
      <button type="button" onClick={() => void filter.apply()}>Apply</button>
      <button type="button" onClick={filter.cancel}>Sort header</button>
      <output>{filter.pending ? "pending" : filter.intent?.sort ?? "none"}</output>
    </>
  );
}

describe("useTaskFilterIntent", () => {
  it("does not apply a late semantic response after a table-header interaction", async () => {
    const request = deferred<FilterIntentResult>();
    mocks.interpretFilterIntent.mockReturnValueOnce(request.promise);
    render(<Harness />);

    await act(async () => { screen.getByRole("button", { name: "Apply" }).click(); });
    expect(screen.getByText("pending")).toBeInTheDocument();

    await act(async () => { screen.getByRole("button", { name: "Sort header" }).click(); });
    await act(async () => request.resolve({
      recognized: true,
      min_score_percent: null,
      max_score_percent: null,
      pass_status: null,
      low_confidence: false,
      review_status: null,
      disagreement: false,
      annotated: false,
      sort: "max_score_desc",
      question_tokens: [],
      question_types: [],
      max_average_confidence: null,
      missing_knowledge: false,
      min_max_score: null,
      max_max_score: null,
      preparation_status: null,
      material_field: null,
      material_status: null,
      submission_status: null,
      text_terms: [],
      explanation: "Sort by maximum score.",
    }));

    expect(screen.getByText("none")).toBeInTheDocument();
    expect(mocks.interpretFilterIntent).toHaveBeenCalledWith("task-1", "show the unusual records", "question_preparation");
  });
});
