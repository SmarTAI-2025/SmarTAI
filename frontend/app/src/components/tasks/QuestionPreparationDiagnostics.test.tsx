import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { JobProgress } from "@/types";
import { QuestionPreparationDiagnostics } from "./QuestionPreparationDiagnostics";

it("counts only required questions and filters yellow issues before the twenty-row cap", () => {
  const warning = { issue_id: "yellow", field: "stem", code: "recognition_needs_review", status: "open", severity: "warning" };
  const red = { issue_id: "red", field: "rubric", code: "generation_failed", status: "open", severity: "blocking" };
  const preparation_review = [
    ...Array.from({ length: 25 }, (_, i) => ({ q_id: `yellow-${i}`, number: `yellow-${i}`, issues: [warning] })),
    ...Array.from({ length: 25 }, (_, i) => ({ q_id: `red-${i}`, number: `red-${i}`, issues: [red, warning] })),
  ];
  render(<QuestionPreparationDiagnostics locale="zh-CN" progress={{ preparation_review } as unknown as JobProgress} />);
  expect(screen.getByText("需要处理 25 题")).toBeVisible();
  expect(screen.getByText("第 red-19 题")).toBeVisible();
  expect(screen.queryByText("第 red-20 题")).not.toBeInTheDocument();
  expect(screen.queryByText(/yellow-/)).not.toBeInTheDocument();
  expect(screen.queryByText(/建议复核/)).not.toBeInTheDocument();
  expect(screen.getAllByRole("list")[0]).toHaveClass("max-h-48", "overflow-y-auto");
});

it("omits the panel when only suggested review remains", () => {
  const { container } = render(<QuestionPreparationDiagnostics locale="en-US" progress={{ preparation_review: [
    { q_id: "q1", number: "1", issues: [{ issue_id: "i1", field: "answer", code: "generation_failed", status: "open", severity: "warning" }] },
  ] } as JobProgress} />);
  expect(container).toBeEmptyDOMElement();
});

it("uses review severity for optional answers and required source conflicts", () => {
  render(<QuestionPreparationDiagnostics locale="zh-CN" progress={{ preparation_review: [
    { q_id: "optional", number: "1.1", issues: [{ issue_id: "a", field: "answer", code: "recognition_partial", status: "open", severity: "blocking", details: { incomplete: true } }] },
    { q_id: "rubric", number: "1.2", issues: [{ issue_id: "b", field: "rubric", code: "source_conflict", status: "open", severity: "warning" }] },
    { q_id: "tests", number: "1.3", issues: [{ issue_id: "c", field: "programming_tests", code: "source_conflict", status: "open", severity: "warning", details: { reason: "test_case_output_conflict" } }] },
  ] } as JobProgress} />);
  expect(screen.getByText("需要处理 2 题")).toBeVisible();
  expect(screen.queryByText("第 1.1 题")).not.toBeInTheDocument();
  expect(screen.getByText("第 1.2 题")).toBeVisible();
  expect(screen.getByText("第 1.3 题")).toBeVisible();
  expect(screen.queryByText(/建议复核：/)).not.toBeInTheDocument();
});
