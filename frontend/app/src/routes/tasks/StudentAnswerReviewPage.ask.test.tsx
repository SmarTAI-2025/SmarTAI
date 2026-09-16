import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StudentAnswerReviewPage } from "./StudentAnswerReviewPage";

const { interpretFilterIntent } = vi.hoisted(() => ({ interpretFilterIntent: vi.fn() }));

const taskData = vi.hoisted(() => ({
  task_id: "task-1",
  name: "SmarTAI Live Demo answer review",
  status: "submissions_ready",
  student_count: 2,
  problem_count: 2,
  problem_data: {
    Q1: { q_id: "Q1", number: "1", type: "Proof", stem: "Question one" },
    Q2: { q_id: "Q2", number: "2", type: "Proof", stem: "Question two" },
  },
  student_data: {
    "DEMO-001": {
      stu_id: "DEMO-001", stu_name: "Alex Chen", source_filename: "DEMO-001_Alex-Chen_typeset.pdf",
      stu_ans: [{ q_id: "Q1", number: "1", content: "Answer one" }, { q_id: "Q2", number: "2", content: "Answer two" }],
    },
    "DEMO-002": {
      stu_id: "DEMO-002", stu_name: "Maya Lin", source_filename: "DEMO-002_Maya-Lin_handwritten.png",
      stu_ans: [{ q_id: "Q1", number: "1", content: "Another answer" }],
    },
  },
}));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ isLoading: false, isError: false, isSuccess: true, data: taskData }),
  useUpdateStudentAnswer: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateStudentIdentity: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));
vi.mock("@/api/analytics", () => ({ interpretFilterIntent }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/components/tasks/PdfDocumentPreview", () => ({ PdfDocumentPreview: () => <div>Student PDF preview</div> }));
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

function renderPage(studentId = "DEMO-001") {
  const router = createMemoryRouter([
    { path: "/tasks/:taskId/students/:studentId", element: <StudentAnswerReviewPage /> },
  ], { initialEntries: [`/tasks/task-1/students/${studentId}`] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("StudentAnswerReviewPage Ask SmarTAI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { callback(0); return 1; });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => (
      { x: 0, y: 0, top: 0, right: 100, bottom: 100, left: 0, width: 100, height: 100, toJSON: () => ({}) }
    ));
  });

  it("filters an exact question locally without calling the model", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.clear(input);
    await user.type(input, "Q2");
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(document.getElementById("answer-question-Q2")).toBeInTheDocument());
    expect(document.getElementById("answer-question-Q1")).not.toBeInTheDocument();
    expect(interpretFilterIntent).not.toHaveBeenCalled();
    expect(screen.getByText("Matched locally; no model call.")).toBeInTheDocument();
  });

  it("uses the model only to translate a free-form request into local controls", async () => {
    const user = userEvent.setup();
    vi.mocked(interpretFilterIntent).mockResolvedValue(recognizedIntent({ submission_status: "missing" }));
    renderPage("DEMO-002");
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.clear(input);
    await user.type(input, "show incomplete answers");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(interpretFilterIntent).toHaveBeenCalledWith("task-1", "show incomplete answers", "student_answer_review"));
    await waitFor(() => expect(document.getElementById("answer-question-Q2")).toBeInTheDocument());
    expect(document.getElementById("answer-question-Q1")).not.toBeInTheDocument();
  });

  it("keeps every question visible when the model cannot translate the full request", async () => {
    const user = userEvent.setup();
    vi.mocked(interpretFilterIntent).mockResolvedValue(recognizedIntent({ sort: "name_asc" }));
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.clear(input);
    await user.type(input, "sort answers by an unsupported property");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(screen.getByText("This student-answer view cannot apply that sort; no partial filter was applied.")).toBeInTheDocument());
    expect(document.getElementById("answer-question-Q1")).toBeInTheDocument();
    expect(document.getElementById("answer-question-Q2")).toBeInTheDocument();
  });

  it("accepts a model-translated reverse question order for this headerless detail view", async () => {
    const user = userEvent.setup();
    vi.mocked(interpretFilterIntent).mockResolvedValue(recognizedIntent({ sort: "question_desc" }));
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.clear(input);
    await user.type(input, "put the current student's questions in reverse order");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(interpretFilterIntent).toHaveBeenCalledWith(
      "task-1", "put the current student's questions in reverse order", "student_answer_review",
    ));
    await waitFor(() => expect(Array.from(document.querySelectorAll("article[data-question-id]")).map((item) => item.getAttribute("data-question-id"))).toEqual(["Q2", "Q1"]));
  });
});
