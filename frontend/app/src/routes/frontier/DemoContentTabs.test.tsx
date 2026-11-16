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
  it.each([true, false])("keeps one copy of a single expert's repeated comment (same=%s)", same => {
    const graded = result();
    const correction = graded.results![0].corrections[0];
    correction.expert_results = [{ provider: "Only model", score: 4, max_score: 5, confidence: .9,
      comment: same ? correction.comment : "Distinct expert explanation", steps: correction.steps }];
    render(view("results", "en-US", graded));
    expect(screen.getAllByText(correction.comment)).toHaveLength(1);
    expect(screen.getByText("Only model")).toBeVisible();
    if (!same) expect(screen.getByText("Distinct expert explanation")).toBeVisible();
  });

  it.each(["zh-CN", "en-US"] as const)("shows additive fractional deductions (%s)", locale => {
    const graded = result();
    const correction = graded.results![0].corrections[0];
    Object.assign(correction, { score: 9.63, provisional_score: 9.63, max_score: 10,
      steps: [{ step_no: 1, part_id: "p", score: 9.63, deduction_base: 10, is_correct: false,
        deductions: [1, 2, 3].map(i => ({ desc: `Equation ${i} loses a solution.`, points: .125 })), desc: "Net" }] });
    render(view("results", locale, graded));
    fireEvent.click(screen.getByText(locale === "zh-CN" ? "扣分点" : "Deductions", { exact: false }));
    const unit = locale === "zh-CN" ? "分" : "pts";
    expect(screen.getAllByText(`-0.12 ${unit}`)).toHaveLength(2);
    expect(screen.getByText(`-0.13 ${unit}`)).toBeVisible();
    expect(screen.getAllByText("9.63 / 10")).toHaveLength(2);
  });

  it.each(["zh-CN", "en-US"] as const)("shows only completed stages and selects newly available content (%s)", locale => {
    const zh = locale === "zh-CN";
    const current = render(view("materials", locale));
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Saved materials" })).toBeVisible();
    current.unmount();
    const submissions = render(view("submissions", locale));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("tab", { name: zh ? "学生作答" : "Student answers" })).toHaveAttribute("aria-selected", "true");
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

  it.each(["zh-CN", "en-US"] as const)("shows original expert evidence instead of synthesized award steps (%s)", locale => {
    const zh = locale === "zh-CN";
    const graded = result();
    const correction = graded.results![0].corrections[0];
    Object.assign(correction, { score: 3.5, provisional_score: 3.5, max_score: 10,
      steps: [{ step_no: 1, score: 3.5, is_correct: false, desc: "Synthetic combined subtotal" }],
      expert_results: [
        { provider: "Reference", score: 0, max_score: 10, confidence: .9, comment: "Reference reasoning", steps: [1, 2, 3].map(n => ({ step_no: n, item_id: `s${n}`, part_id: "p", score: 0, is_correct: false, desc: `Missing reference obligation ${n}` })) },
        { provider: "Alternative", score: 7, max_score: 10, confidence: .9, comment: "Alternative reasoning", steps: [{ step_no: 1, part_id: "p", score: 7, is_correct: false, is_alternative: true, desc: "Net subtotal", deductions: [1, 2, 3].map(n => ({ desc: `Wrong sign in equation ${n}.`, points: 1 })) }] },
      ],
    });
    render(view("results", locale, graded));
    expect(screen.getAllByText("3.5 / 10")).toHaveLength(2);
    expect(screen.queryByText("Synthetic combined subtotal")).not.toBeInTheDocument();
    const alternative = screen.getByText("Alternative", { exact: true }).closest("section")!;
    const reference = screen.getByText("Reference", { exact: true }).closest("section")!;
    expect(within(alternative).getByText(zh ? "另解批改" : "Alternative method")).toBeVisible();
    expect(within(reference).queryByText(zh ? "另解批改" : "Alternative method")).not.toBeInTheDocument();
    fireEvent.click(within(alternative).getByText(zh ? "扣分点" : "Deductions", { exact: false }));
    expect(within(alternative).getAllByText(zh ? "-1 分" : "-1 pts")).toHaveLength(3);
    expect(within(alternative).getByText("Wrong sign in equation 2.")).toBeVisible();
    expect(screen.queryByText("Net subtotal")).not.toBeInTheDocument();
    fireEvent.click(within(reference).getByText(zh ? "评分步骤" : "Scoring steps", { exact: false }));
    expect(within(reference).getAllByText(zh ? "0 分" : "0 pts")).toHaveLength(3);
  });

  it.each([false, true])("labels full-credit alternatives without inventing a correct step (expert=%s)", expert => {
    const graded = result();
    const correction = graded.results![0].corrections[0];
    correction.steps = [{ step_no: 1, score: 5, is_correct: true, is_alternative: true, desc: "Full-credit subtotal", deductions: [] }];
    if (expert) correction.expert_results = [{ provider: "Original model", score: 5, max_score: 5, confidence: .9, comment: "Correct equivalences", steps: correction.steps }];
    render(view("results", "en-US", graded));
    expect(screen.getByText("Alternative method")).toBeVisible();
    expect(screen.queryByText("Full-credit subtotal")).not.toBeInTheDocument();
    expect(screen.queryByText(/Scoring steps|Deductions/)).not.toBeInTheDocument();
  });
});
