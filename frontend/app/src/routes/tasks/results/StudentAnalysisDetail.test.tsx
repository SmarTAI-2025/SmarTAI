import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";
import { buildResultsModel } from "@/components/tasks/resultsModel";
import { StudentAnalysisDetail } from "./StudentAnalysisDetail";

it("preserves a precise maximum in original and applied result panels", () => {
  const model = buildResultsModel(undefined, {
    status: "completed", task_id: "task",
    results: [{ student_id: "S1", corrections: [{
      q_id: "q1", type: "reasoning", score: 2.55, provisional_score: 2.55,
      max_score: 2.55, confidence: .9, comment: "Original rationale", steps: [],
      expert_results: [], requires_human_review: false, review_reasons: [],
    }] }],
  });
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter>
    <StudentAnalysisDetail locale="en-US" taskId="task" studentId="S1" model={model} />
  </MemoryRouter></QueryClientProvider>);
  for (const title of ["Original SmarTAI Result", "Final Applied Result"]) {
    const panel = screen.getByRole("heading", { name: title }).closest("section")!;
    expect(within(panel).getByText("2.55 / 2.55")).toBeInTheDocument();
  }
  expect(screen.getByText(/Final 2.55 \/ 2.55/)).toBeInTheDocument();
  expect(screen.queryByText(/2.6 \/ 2.6/)).not.toBeInTheDocument();
});
