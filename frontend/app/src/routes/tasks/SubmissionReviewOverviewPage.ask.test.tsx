import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";

const { interpretFilterIntent } = vi.hoisted(() => ({ interpretFilterIntent: vi.fn() }));

const taskData = vi.hoisted(() => ({
  task_id: "task-1",
  name: "SmarTAI Live Demo submissions",
  status: "submissions_ready",
  student_data: {
    "S-001": {
      stu_id: "S-001", stu_name: "Alex Chen",
      stu_ans: [{ q_id: "Q1", number: "1", content: "Complete answer" }, { q_id: "Q2", number: "2", content: "Complete answer" }],
    },
    "S-002": {
      stu_id: "S-002", stu_name: "Maya Lin",
      stu_ans: [{ q_id: "Q1", number: "1", content: "Complete answer" }],
    },
  },
  problem_data: {
    Q1: { q_id: "Q1", number: "1", type: "Proof", stem: "Question one" },
    Q2: { q_id: "Q2", number: "2", type: "Calculation", stem: "Question two" },
  },
}));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ isLoading: false, isError: false, isSuccess: true, data: taskData }),
}));
vi.mock("@/api/analytics", () => ({ interpretFilterIntent }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/components/tasks/MatrixQueueWorkspace", () => ({ MatrixQueueWorkspace: ({ matrix }: { matrix: React.ReactNode }) => <>{matrix}</> }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US", t: (key: string) => key }) }));

function recognizedIntent(overrides: Record<string, unknown> = {}) {
  return {
    recognized: true,
    min_score_percent: null,
    max_score_percent: null,
    pass_status: null,
    low_confidence: false,
    review_status: null,
    disagreement: false,
    annotated: false,
    sort: null,
    question_tokens: [],
    text_terms: [],
    explanation: "",
    ...overrides,
  };
}

function renderPage() {
  render(
    <MemoryRouter initialEntries={["/tasks/task-1/submissions"]}>
      <Routes><Route path="/tasks/:taskId/submissions" element={<SubmissionReviewOverviewPage />} /></Routes>
    </MemoryRouter>,
  );
}

describe("SubmissionReviewOverviewPage Ask SmarTAI", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps an exact question request local", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = await screen.findByPlaceholderText("Ask SmarTAI: show missing answers, or sort by coverage");

    await user.type(input, "Q2");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(screen.getByText("Matched locally; no model call.")).toBeInTheDocument());
    expect(interpretFilterIntent).not.toHaveBeenCalled();
    const matrix = screen.getByRole("table");
    expect(within(matrix).getByRole("button", { name: /Calculation:/ })).toBeInTheDocument();
    expect(within(matrix).queryByRole("button", { name: /Proof:/ })).not.toBeInTheDocument();
  });

  it("uses model-translated controls for a free-form missing-answer request", async () => {
    const user = userEvent.setup();
    vi.mocked(interpretFilterIntent).mockResolvedValue(recognizedIntent({ submission_status: "missing" }));
    renderPage();
    const input = await screen.findByPlaceholderText("Ask SmarTAI: show missing answers, or sort by coverage");

    await user.type(input, "show incomplete answers");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(interpretFilterIntent).toHaveBeenCalledWith("task-1", "show incomplete answers", "submission_review"));
    await waitFor(() => expect(screen.getByText("S-002")).toBeInTheDocument());
    expect(screen.queryByText("S-001")).not.toBeInTheDocument();
  });

  it("reverses the student order when the same question header is clicked twice", async () => {
    const user = userEvent.setup();
    renderPage();
    const q2Header = await screen.findByRole("button", { name: /Calculation:/ });

    await user.click(q2Header);
    expect(q2Header.closest("th")).toHaveAttribute("aria-sort", "ascending");
    await user.click(q2Header);
    expect(q2Header.closest("th")).toHaveAttribute("aria-sort", "descending");

    await waitFor(() => {
      const firstStudentRow = document.querySelector("tbody tr");
      expect(firstStudentRow).toHaveTextContent("S-002");
    });
  });
});
