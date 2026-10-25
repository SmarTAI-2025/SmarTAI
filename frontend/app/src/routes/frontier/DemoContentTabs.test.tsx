import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { TaskResultResponse } from "@/types";
import type { Locale } from "@/i18n/messages";
import { DemoContentTabs, type DemoContentStage } from "./DemoContentTabs";

function result(): TaskResultResponse {
  return {
    status: "completed", task_id: "demo",
    problem_data: { q1: { q_id: "q1", number: "7.2", type: "calculation", stem: "DO NOT REPEAT THE QUESTION", criterion: "rubric", max_score: 5 } },
    results: [{ student_id: "s1", student_name: "Alex Chen", student_answers: [{ q_id: "q1", content: "DO NOT REPEAT THE ANSWER", number: "7.2", type: "calculation", flag: [] }], corrections: [{
      q_id: "q1", type: "calculation", score: 4, provisional_score: 4, max_score: 5, comment: "Correct substitution; bounds not explained.",
      confidence: 0.9, requires_human_review: false, review_reasons: [], expert_results: [],
      steps: [{ step_no: 1, desc: "Substitution credit", score: 2, is_correct: true }, { step_no: 2, desc: "Bounds credit", score: 0, is_correct: false }],
    }] }],
  };
}

function view(stage: DemoContentStage, locale: Locale = "en-US", gradingResult = stage === "results" ? result() : null) {
  return <MemoryRouter><DemoContentTabs stage={stage} locale={locale} taskId="demo" result={gradingResult}
    materials={<h3>Saved materials</h3>} submissions={<h3>Saved submissions</h3>} insights={<h3>Existing analysis</h3>} /></MemoryRouter>;
}

describe("DemoContentTabs", () => {
  it.each(["zh-CN", "en-US"] as const)("shows only completed stages and selects newly available content (%s)", locale => {
    const zh = locale === "zh-CN";
    const current = render(view("materials", locale));
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Saved materials" })).toBeVisible();
    current.unmount();
    const submissions = render(view("submissions", locale));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("tab", { name: zh ? "学生作答" : "Submissions" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "Saved submissions" })).toBeVisible();
    submissions.unmount();
    render(view("results", locale));
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.getByRole("tab", { name: zh ? "批改详情" : "Grading details" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: zh ? "本次批改的具体情况" : "Detailed grading results" })).toBeVisible();
  });

  it("keeps the user's tab and expanded steps when props refresh or another tab is selected", () => {
    const current = render(view("results"));
    const details = screen.getByText("Scoring steps", { exact: false }).closest("details")!;
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(within(details).getByText("Scoring steps", { exact: false }));
    expect(details).toHaveAttribute("open");
    fireEvent.click(screen.getByRole("tab", { name: "Materials" }));
    current.rerender(view("results"));
    expect(screen.getByRole("tab", { name: "Materials" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "Grading details" }));
    expect(details).toHaveAttribute("open");
  });

  it("shows each student's scores, comments and folded steps without question or answer bodies", () => {
    render(view("results"));
    expect(screen.getByText("Alex Chen")).toBeVisible();
    expect(screen.getByText("Q7.2")).toBeVisible();
    expect(screen.getByText("Correct substitution; bounds not explained.")).toBeVisible();
    expect(screen.queryByText("DO NOT REPEAT THE QUESTION")).not.toBeInTheDocument();
    expect(screen.queryByText("DO NOT REPEAT THE ANSWER")).not.toBeInTheDocument();
    expect(screen.queryByText("Substitution credit")).not.toBeVisible();
    fireEvent.click(screen.getByText("Scoring steps", { exact: false }));
    expect(screen.getByText("Substitution credit")).toBeVisible();
    expect(screen.getByText("0 pts")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open grading review" })).toHaveAttribute("href", "/tasks/demo/review");
  });

  it("uses the review page's AI score semantics and never turns a missing score into zero", () => {
    const graded = result();
    const correction = graded.results![0].corrections[0];
    correction.score = 5;
    correction.teacher_score = 5;
    correction.provisional_score = 0;
    const current = render(view("results", "en-US", graded));
    expect(screen.getAllByText("0 / 5")).toHaveLength(2);
    correction.synthesis_method = "all_failed";
    current.rerender(view("results", "en-US", graded));
    expect(screen.getByText("No valid score")).toBeVisible();
    expect(screen.queryByText("0 / 5")).not.toBeInTheDocument();
  });

  it("supports keyboard navigation and links each active tab to its panel", () => {
    render(view("results"));
    const list = screen.getByRole("tablist");
    fireEvent.keyDown(list, { key: "ArrowRight" });
    const analysis = screen.getByRole("tab", { name: "Learning analysis" });
    expect(analysis).toHaveFocus();
    expect(analysis).toHaveAttribute("aria-selected", "true");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", analysis.getAttribute("aria-controls"));
    expect(panel).toHaveAttribute("aria-labelledby", analysis.id);
    fireEvent.keyDown(list, { key: "Home" });
    expect(screen.getByRole("tab", { name: "Materials" })).toHaveFocus();
    fireEvent.keyDown(list, { key: "End" });
    expect(analysis).toHaveFocus();
  });
});
