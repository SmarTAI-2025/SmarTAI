import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import type { QuestionSummary, ResultsModel, StudentSummary } from "@/components/tasks/resultsModel";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { FilterIntentResult } from "@/types";
import { StudentAnalysisOverview } from "./StudentAnalysisOverview";

vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));

function correction(qId: string, score: number) {
  return {
    q_id: qId, type: "calculation", score, max_score: 10, confidence: 0.9, comment: "",
    steps: [], expert_results: [], requires_human_review: false, review_reasons: [],
  };
}

const zoe: StudentSummary = {
  id: "S-001", name: "Zoe", corrections: [correction("Q1", 3)], answers: [], answerByQuestion: new Map(),
  totalScore: 3, totalMax: 10, percent: 30, avgConfidence: 0.9, lowConfidenceCount: 0, reviewCount: 0,
};
const amy: StudentSummary = {
  id: "S-002", name: "Amy", corrections: [correction("Q1", 9)], answers: [], answerByQuestion: new Map(),
  totalScore: 9, totalMax: 10, percent: 90, avgConfidence: 0.9, lowConfidenceCount: 0, reviewCount: 0,
};
const question: QuestionSummary = {
  id: "Q1", label: "Q1", type: "calculation", entries: [], count: 2, avgScore: 6, maxScore: 10,
  avgPercent: 60, minScore: 3, maxObservedScore: 9, lowConfidenceCount: 0, reviewCount: 0,
};
const model: ResultsModel = {
  problems: [], students: [zoe, amy], questions: [question], classAverageScore: 6, classAverageMax: 10,
  classAveragePercent: 60, lowConfidenceCount: 0, reviewCount: 0,
};

function semanticIntent(overrides: Partial<FilterIntentResult> = {}): FilterIntentResult {
  return {
    recognized: true, min_score_percent: null, max_score_percent: null, pass_status: null,
    low_confidence: false, review_status: null, disagreement: false, annotated: false, sort: null,
    question_tokens: [], text_terms: [], explanation: "Model interpreted the instruction.", ...overrides,
  };
}

function mount() {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={["/tasks/task-1/results/students"]}>
        <StudentAnalysisOverview locale="en-US" taskId="task-1" model={model} />
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("StudentAnalysisOverview Ask SmarTAI", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps a header sort when an older semantic response arrives", async () => {
    let finish!: (value: FilterIntentResult) => void;
    vi.mocked(interpretFilterIntent).mockReturnValue(new Promise<FilterIntentResult>((resolve) => { finish = resolve; }));
    mount();

    const input = screen.getByRole("textbox", { name: "Ask SmarTAI: understand student analysis" });
    fireEvent.change(input, { target: { value: "show students who need more coaching" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(interpretFilterIntent).toHaveBeenCalledWith(
      "task-1", "show students who need more coaching", "student_analysis",
    ));

    const rateHeader = screen.getByRole("button", { name: "Sort by rate: currently unsorted; click to sort ascending" });
    fireEvent.click(rateHeader);
    expect(rateHeader.closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(document.querySelector("tbody tr")).toHaveTextContent("Zoe");

    await act(async () => finish(semanticIntent({ sort: "score_desc" })));

    expect(rateHeader.closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(document.querySelector("tbody tr")).toHaveTextContent("Zoe");
  });
});
