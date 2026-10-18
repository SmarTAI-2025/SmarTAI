import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { buildResultsModel } from "@/components/tasks/resultsModel";
import type { Locale } from "@/i18n/messages";
import type { AICompletionProvenance, MaterialFieldProvenance, ProblemInfo, TestCase } from "@/types";
import fixtures from "@/test/fixtures/questionAnalysisMaterials.json";
import { QuestionAnalysisDetail } from "./QuestionAnalysisDetail";

const base: ProblemInfo = {
  q_id: "q1", number: "1", type: "programming", stem: "What is 4 + 4?",
  criterion: "Eight", max_score: 10, reference_answer: "8", review_status: "needs_review",
  test_cases: fixtures.functionCases as TestCase[],
};

function mount(problem: ProblemInfo = base, locale: Locale = "en-US") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function tree(current: ProblemInfo) {
    const model = buildResultsModel(undefined, {
      task_id: "materials", status: "completed", results: [],
      problem_data: { [current.q_id]: current },
    });
    return <QueryClientProvider client={client}><MemoryRouter>
      <QuestionAnalysisDetail locale={locale} taskId="materials" questionId={current.q_id} model={model} />
    </MemoryRouter></QueryClientProvider>;
  }
  const view = render(tree(problem));
  return { update: (current: ProblemInfo) => view.rerender(tree(current)) };
}

function panel(title = "Code / test materials") {
  return within(screen.getByRole("heading", { name: title }).closest("section")!);
}

describe("question analysis test materials", () => {
  it.each(["en-US", "zh-CN"] as const)("makes all six function cases accessible with keyboard expansion (%s)", async locale => {
    mount(base, locale);
    const zh = locale === "zh-CN";
    const materials = panel(zh ? "代码 / 测试资料" : "Code / test materials");
    expect(materials.getByText(zh ? "前 2 项 / 共 6 项" : "First 2 of 6 test cases")).toBeInTheDocument();
    expect(materials.getAllByRole("article")).toHaveLength(2);
    const expand = materials.getByRole("button", { name: zh ? "查看全部 6 项" : "View all 6 test cases" });
    expand.focus();
    await userEvent.keyboard("{Enter}");
    expect(expand).toHaveAttribute("aria-expanded", "true");
    expect(materials.getAllByRole("article")).toHaveLength(6);
    fixtures.functionCases.forEach((testCase, index) => {
      const item = within(materials.getByRole("article", { name: zh ? `测试样例 ${index + 1}` : `Test case ${index + 1}` }));
      expect(item.getByText("synthetic_function", { exact: true })).toBeInTheDocument();
      expect(item.getByText(JSON.stringify(testCase.function_args, null, 2), { exact: true, normalizer: text => text })).toBeInTheDocument();
      expect(item.getByText(testCase.expected_return, { exact: true })).toBeInTheDocument();
      expect(item.queryByText(zh ? "输入" : "Input", { exact: true })).not.toBeInTheDocument();
    });
    expect(materials.getByText(zh ? "共 6 个测试样例" : "6 test cases")).toBeInTheDocument();
    await userEvent.keyboard(" ");
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(materials.getAllByRole("article")).toHaveLength(2);
  });

  it("distinguishes no arguments and empty strings from missing function fields, preserving nested/multiple arguments", () => {
    mount({ ...base, test_cases: fixtures.functionBoundaries as TestCase[] });
    fireEvent.click(panel().getByRole("button", { name: "View all 3 test cases" }));
    const cases = panel().getAllByRole("article").map(item => within(item));
    expect(cases[0].getByText("[]", { exact: true })).toBeInTheDocument();
    expect(cases[0].getByText('""', { exact: true })).toBeInTheDocument();
    expect(cases[0].queryByText("Not provided")).not.toBeInTheDocument();
    expect(cases[1].getByText(JSON.stringify(fixtures.functionBoundaries[1].function_args, null, 2), { normalizer: text => text })).toBeInTheDocument();
    expect(cases[1].getByText("{'ok': False}", { exact: true })).toBeInTheDocument();
    expect(cases[2].getAllByText("Not provided", { exact: true })).toHaveLength(3);
  });

  it.each([1, 2, 6])("preserves stdin input/output, whitespace, empty strings and solution code (%i cases)", count => {
    mount({ ...base, test_cases: fixtures.stdinCases.slice(0, count) as TestCase[], solution_code: "def solve():\\n    print(8)" });
    const materials = panel();
    expect(materials.getAllByRole("article")).toHaveLength(Math.min(2, count));
    if (count > 2) fireEvent.click(materials.getByRole("button", { name: `View all ${count} test cases` }));
    else expect(materials.queryByRole("button")).not.toBeInTheDocument();
    expect(materials.getAllByRole("article")).toHaveLength(count);
    expect(materials.getByText(`${count} test cases`)).toBeInTheDocument();
    fixtures.stdinCases.slice(0, count).forEach((testCase, index) => {
      const item = materials.getAllByRole("article")[index];
      const values = [...item.querySelectorAll("dd")].map(node => node.textContent);
      expect(values).toEqual([testCase.input === "" ? '""' : testCase.input, testCase.expected_output === "" ? '""' : testCase.expected_output]);
      expect(within(item).getByText("Input", { exact: true })).toBeInTheDocument();
      expect(within(item).getByText("Expected output", { exact: true })).toBeInTheDocument();
    });
    expect(materials.queryByText("ignored_function")).not.toBeInTheDocument();
    expect(materials.queryByText("ignored_return")).not.toBeInTheDocument();
    expect(materials.getByText("def solve():\n    print(8)", { normalizer: text => text })).toBeInTheDocument();
  });

  it("keeps legacy stdin cases without io_mode readable", () => {
    const { io_mode: _mode, ...legacy } = fixtures.stdinCases[0];
    mount({ ...base, test_cases: [legacy as TestCase] });
    expect(panel().getByText("4 4", { exact: true })).toBeInTheDocument();
    expect(panel().getByText("Input", { exact: true })).toBeInTheDocument();
  });

  it.each([undefined, null, []])("keeps the empty material message (%s)", test_cases => {
    mount({ ...base, test_cases });
    expect(panel().getByText("This material is not included in the final results.")).toBeInTheDocument();
    expect(panel().queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows solution code even when there are no test cases", () => {
    mount({ ...base, test_cases: [], solution_code: "def solve():\n    return 8" });
    expect(panel().getByText("def solve():\n    return 8", { normalizer: text => text })).toBeInTheDocument();
    expect(panel().getByText("This material is not included in the final results.")).toBeInTheDocument();
  });

  it("resets the preview when navigating to a different question", () => {
    const view = mount();
    fireEvent.click(panel().getByRole("button", { name: "View all 6 test cases" }));
    view.update({ ...base, q_id: "q2", number: "2" });
    expect(panel().getAllByRole("article")).toHaveLength(2);
    expect(panel().getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });
});

describe.each(["en-US", "zh-CN"] as const)("material source does not assert version confirmation (%s)", locale => {
  it.each(["ai", "imported", "recognized", "teacher"] as const)("keeps %s source independent through confirmation, editing and regeneration", source => {
    const ai: AICompletionProvenance = { job_id: "synthetic", candidate_id: "synthetic", source_kind: "ai_generated", provider_id: "synthetic", review_status: "pending", generated_at: 1, updated_at: 1 };
    const imported: MaterialFieldProvenance = { import_job_id: "synthetic", candidate_id: "synthetic", source_kind: "upload", source_filename: "synthetic.txt", confidence: 1, match_status: "exact", source_excerpt: "8", source_location: "1", reason: "Exact match", review_status: "pending", imported_at: 1, updated_at: 1 };
    const fields = ["reference_answer", "test_cases"] as const;
    const problem: ProblemInfo = {
      ...base,
      test_cases: (source === "teacher" ? fixtures.stdinCases : fixtures.functionCases) as TestCase[],
      ...(source === "ai" ? { ai_completion_provenance: { reference_answer: ai, test_cases: ai } } : {}),
      ...(source === "imported" ? { material_provenance: { reference_answer: imported, test_cases: imported } } : {}),
    };
    const view = mount(problem, locale);
    const zh = locale === "zh-CN";
    const label = source === "ai" ? (zh ? "SmarTAI 补全" : "SmarTAI completion") : source === "imported"
      ? (zh ? "资料导入 · synthetic.txt" : "Imported · synthetic.txt") : (zh ? "题目识别 / 教师维护" : "Recognition / teacher maintained");
    // Neither old provenance nor a question-level confirmed flag proves that
    // subsequently changed material is the version the teacher confirmed.
    for (const state of ["pending", "confirmed", "edited", "regenerated", "stale-confirmed"] as const) {
      const review_status = state === "confirmed" || state === "stale-confirmed" ? "confirmed" : state === "edited" ? "edited" : "needs_review";
      const provenanceStatus = state === "confirmed" || state === "stale-confirmed" ? "confirmed" : state === "edited" ? "edited" : "pending";
      const changed = state === "edited" || state === "regenerated" || state === "stale-confirmed";
      view.update({ ...problem, review_status, max_score_review_status: "confirmed", preparation_issues: [],
        reference_answer: changed ? "Updated answer" : "8",
        test_cases: changed ? fixtures.functionBoundaries as TestCase[] : problem.test_cases,
        ...(source === "ai" ? { ai_completion_provenance: Object.fromEntries(fields.map(field => [field, { ...ai, review_status: provenanceStatus, updated_at: changed ? 2 : 1 }])) } : {}),
        ...(source === "imported" ? { material_provenance: Object.fromEntries(fields.map(field => [field, { ...imported, review_status: provenanceStatus, updated_at: changed ? 2 : 1 }])) } : {}),
      });
      for (const title of zh ? ["标答 / 参考答案", "代码 / 测试资料"] : ["Reference answer", "Code / test materials"]) {
        const material = panel(title);
        expect(material.getByText(label, { exact: true })).toBeInTheDocument();
        expect(material.queryByText(/teacher-confirmed|教师已确认/)).not.toBeInTheDocument();
      }
    }
  });
});
