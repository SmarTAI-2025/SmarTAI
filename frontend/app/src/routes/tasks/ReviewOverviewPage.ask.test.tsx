import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import type { FilterIntentResult, Task, TaskResultResponse } from "@/types";
import { ReviewOverviewPage } from "./ReviewOverviewPage";

vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/components/tasks/MatrixQueueWorkspace", () => ({
  MatrixQueueWorkspace: ({ matrix }: { matrix: React.ReactNode }) => <>{matrix}</>,
}));
vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US" }),
}));

const task = {
  task_id: "task-1", name: "Review task", owner_id: "teacher", status: "graded", workflow_revision: 1,
  problem_count: 1, student_count: 2, kb_docs: {}, kb_doc_count: 0, created_at: 0, updated_at: 0,
  problem_data: { Q1: { q_id: "Q1", number: "1", type: "calculation", stem: "Question one" } },
  student_data: {},
} as unknown as Task;
const result = {
  status: "completed", task_id: "task-1", problem_data: task.problem_data, results: [
    {
      student_id: "S-001", student_name: "Zoe", corrections: [{
        q_id: "Q1", type: "calculation", score: 3, max_score: 10, confidence: 0.9, comment: "",
        steps: [], expert_results: [], requires_human_review: false, review_reasons: [],
      }],
    },
    {
      student_id: "S-002", student_name: "Amy", corrections: [{
        q_id: "Q1", type: "calculation", score: 9, max_score: 10, confidence: 0.9, comment: "",
        steps: [], expert_results: [], requires_human_review: false, review_reasons: [],
      }],
    },
  ],
} as unknown as TaskResultResponse;

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: task, isLoading: false, isError: false }),
  useTaskResult: () => ({ data: result, isLoading: false, isError: false }),
  useTeacherComments: () => ({ data: { comments: {} }, isLoading: false, isError: false, refetch: vi.fn() }),
  useTaskFinalization: () => ({ data: { remaining_review_count: 0, ready_for_confirmation: true }, isLoading: false, isError: false, refetch: vi.fn() }),
  useConfirmTaskFinalization: () => ({ isPending: false, isError: false, mutate: vi.fn() }),
}));

function semanticIntent(overrides: Partial<FilterIntentResult> = {}): FilterIntentResult {
  return {
    recognized: true, min_score_percent: null, max_score_percent: null, pass_status: null,
    low_confidence: false, review_status: null, disagreement: false, annotated: false, sort: null,
    question_tokens: [], text_terms: [], explanation: "Model interpreted the instruction.", ...overrides,
  };
}

function mount() {
  render(
    <MemoryRouter initialEntries={["/tasks/task-1/review"]}>
      <Routes><Route path="/tasks/:taskId/review" element={<ReviewOverviewPage />} /></Routes>
    </MemoryRouter>,
  );
}

describe("ReviewOverviewPage Ask SmarTAI", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps a header sort and the full matrix after a stale semantic response", async () => {
    let finish!: (value: FilterIntentResult) => void;
    vi.mocked(interpretFilterIntent).mockReturnValue(new Promise<FilterIntentResult>((resolve) => { finish = resolve; }));
    mount();

    const input = await screen.findByPlaceholderText("For example: students below 90, model disagreement, Q2, or high to low…");
    fireEvent.change(input, { target: { value: "show the students who need a teaching plan" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(interpretFilterIntent).toHaveBeenCalledWith(
      "task-1", "show the students who need a teaching plan", "review_overview",
    ));

    const idHeader = screen.getByRole("button", { name: "Student ID: currently unsorted; click to sort ascending" });
    fireEvent.click(idHeader);
    expect(idHeader.closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(document.querySelector("tbody tr")).toHaveTextContent("S-001");

    await act(async () => finish(semanticIntent({ text_terms: ["Amy"] })));

    expect(idHeader.closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(document.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(document.querySelector("tbody tr")).toHaveTextContent("S-001");
  });
});
