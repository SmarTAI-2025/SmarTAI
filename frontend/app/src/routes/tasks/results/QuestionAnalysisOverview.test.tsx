import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import type { QuestionSummary, ResultsModel } from "@/components/tasks/resultsModel";
import type { FilterIntentResult } from "@/types";
import { parseSemanticQuestionQuery, QuestionAnalysisOverview } from "./QuestionAnalysisOverview";

vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));

const question = (id: string, percent: number, type: string): QuestionSummary => ({
  id, label: id, type, stem: `Stem ${id}`, entries: [], count: 4,
  avgScore: percent / 10, maxScore: 10, avgPercent: percent, minScore: 0,
  maxObservedScore: 10, lowConfidenceCount: 0, reviewCount: 0,
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
function mount() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
    <MemoryRouter><QuestionAnalysisOverview locale="zh-CN" taskId="task-demo" model={model} /></MemoryRouter>
  </QueryClientProvider>);
}
function submit(value: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "Ask SmarTAI：题目分析" }), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Ask SmarTAI" }));
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

  it("routes a partially understood natural-language request once and applies type aliases and ordering", async () => {
    vi.mocked(interpretFilterIntent).mockResolvedValue(intent({ question_types: ["calculation"], sort: "score_asc" }));
    mount();
    submit("计算题按大家表现排一下，最差的先看");
    await waitFor(() => expect(visibleQuestions()).toEqual(["Q2", "Q1"]));
    expect(interpretFilterIntent).toHaveBeenCalledExactlyOnceWith("task-demo", "计算题按大家表现排一下，最差的先看", "question_analysis", expect.any(AbortSignal));
  });

  it("does not apply a recognized fragment after the model rejects the complete instruction", async () => {
    vi.mocked(interpretFilterIntent).mockResolvedValue(intent({ recognized: false, max_score_percent: 70, explanation: "无法按未提供的出勤率筛选。" }));
    mount();
    submit("得分率低于70%且上课缺勤最多的题目");
    expect(await screen.findByText(/未应用部分筛选/)).toBeInTheDocument();
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
