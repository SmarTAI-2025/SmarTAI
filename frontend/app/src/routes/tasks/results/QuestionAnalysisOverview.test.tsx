import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import type { QuestionSummary, ResultsModel } from "@/components/tasks/resultsModel";
import type { FilterIntentResult } from "@/types";
import { parseSemanticQuestionQuery, QuestionAnalysisOverview } from "./QuestionAnalysisOverview";

vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));

const question = (id: string, percent: number, type: string, maxScore = 10, count = 4): QuestionSummary => ({
  id, label: id, type, stem: `Stem ${id}`, entries: [], count,
  avgScore: percent * maxScore / 100, maxScore, avgPercent: percent, minScore: 0,
  maxObservedScore: maxScore, lowConfidenceCount: 0, reviewCount: 0,
});
const model: ResultsModel = {
  problems: [], students: [], questions: [question("Q1", 70, "计算题"), question("Q2", 40, "calculation"), question("Q11", 90, "proof")],
  classAverageScore: 6, classAverageMax: 10, classAveragePercent: 60, lowConfidenceCount: 0, reviewCount: 0,
};
function intent(overrides: Partial<FilterIntentResult> = {}): FilterIntentResult {
  return { recognized: true, min_score_percent: null, max_score_percent: null, pass_status: null,
    low_confidence: false, review_status: null, disagreement: false, annotated: false, sort: null,
    question_tokens: [], text_terms: [], explanation: "已理解", ...overrides };
}
function mount(resultsModel = model) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
    <MemoryRouter><QuestionAnalysisOverview locale="zh-CN" taskId="task-demo" model={resultsModel} /></MemoryRouter>
  </QueryClientProvider>);
}
function submit(value: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "智能筛选题目" }), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
}
function visibleQuestions() {
  return within(screen.getByRole("table")).getAllByRole("row").slice(1).map((row) => row.querySelector("strong")?.textContent);
}

describe("question analysis Ask SmarTAI", () => {
  beforeEach(() => vi.clearAllMocks());

  it("does not misread the English word questions as a Q-prefixed question number", () => {
    expect(parseSemanticQuestionQuery("questions", "en-US").qTokens).toEqual([]);
  });

  it("keeps a fully understood Q1 shortcut local without also selecting Q11", () => {
    mount();
    submit("Q1");
    expect(visibleQuestions()).toEqual(["Q1"]);
    expect(interpretFilterIntent).not.toHaveBeenCalled();
  });

  it("sorts by full marks locally without dropping any question", () => {
    mount({
      ...model,
      questions: [question("Q20", 70, "calculation", 20), question("Q5", 70, "proof", 5)],
    });

    submit("按满分升序");

    expect(visibleQuestions()).toEqual(["Q5", "Q20"]);
    expect(interpretFilterIntent).not.toHaveBeenCalled();
  });

  it("routes a partially understood natural-language request once and applies type aliases and ordering", async () => {
    vi.mocked(interpretFilterIntent).mockResolvedValue(intent({ question_types: ["calculation"], sort: "score_asc" }));
    mount();
    submit("计算题按大家表现排一下，最差的先看");
    await waitFor(() => expect(visibleQuestions()).toEqual(["Q2", "Q1"]));
    expect(interpretFilterIntent).toHaveBeenCalledExactlyOnceWith("task-demo", "计算题按大家表现排一下，最差的先看", "question_analysis");
  });

  it("applies a structured response-count sort after an unfamiliar phrasing", async () => {
    vi.mocked(interpretFilterIntent).mockResolvedValue(intent({ sort: "coverage_desc" }));
    mount({
      ...model,
      questions: [question("Q1", 70, "calculation", 10, 1), question("Q2", 70, "proof", 10, 8)],
    });

    submit("把交得最多的题先列出来");

    await waitFor(() => expect(visibleQuestions()).toEqual(["Q2", "Q1"]));
    expect(interpretFilterIntent).toHaveBeenCalledExactlyOnceWith("task-demo", "把交得最多的题先列出来", "question_analysis");
  });

  it("does not apply a recognized fragment after the model rejects the complete instruction", async () => {
    vi.mocked(interpretFilterIntent).mockResolvedValue(intent({ recognized: false, max_score_percent: 70, explanation: "无法按未提供的出勤率筛选。" }));
    mount();
    submit("得分率低于70%且上课缺勤最多的题目");
    expect(await screen.findByText(/未能完整转换指令，未应用部分条件/)).toBeInTheDocument();
    expect(visibleQuestions()).toEqual(["Q1", "Q2", "Q11"]);
    expect(interpretFilterIntent).toHaveBeenCalledTimes(1);
  });

  it("shows a model failure and retries only when explicitly requested", async () => {
    vi.mocked(interpretFilterIntent).mockRejectedValueOnce(new Error("upstream unavailable"));
    mount();
    submit("把需要我重新讲解的先列出来");
    const retry = await screen.findByRole("button", { name: "重新尝试" });
    expect(interpretFilterIntent).toHaveBeenCalledTimes(1);
    vi.mocked(interpretFilterIntent).mockResolvedValueOnce(intent({ sort: "score_asc" }));
    fireEvent.click(retry);
    await waitFor(() => expect(visibleQuestions()).toEqual(["Q2", "Q1", "Q11"]));
    expect(interpretFilterIntent).toHaveBeenCalledTimes(2);
  });
});
