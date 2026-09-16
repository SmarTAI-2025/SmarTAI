import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FilterIntentResult } from "@/types";
import { StudentAnswerReviewPage } from "./StudentAnswerReviewPage";

const filterIntentMocks = vi.hoisted(() => ({
  mutate: vi.fn(),
  reset: vi.fn(),
}));

const sourcePreviewMocks = vi.hoisted(() => ({
  openPreview: vi.fn(),
  closePreview: vi.fn(),
  retryPreview: vi.fn(),
}));

const taskData = vi.hoisted(() => ({
  task_id: "task-1",
  name: "Calculus review",
  owner_id: "teacher-1",
  status: "submissions_ready",
  workflow_revision: 3,
  student_count: 1,
  problem_count: 2,
  kb_docs: {},
  kb_doc_count: 0,
  created_at: 1,
  updated_at: 1,
  problem_data: {
    Q1: {
      q_id: "Q1",
      number: "1",
      type: "Proof",
      stem: "Question one",
      review_status: "confirmed",
    },
    Q2: {
      q_id: "Q2",
      number: "2",
      type: "Proof",
      stem: "Question two",
      review_status: "confirmed",
    },
  },
  student_data: {
    S001: {
      stu_id: "S001",
      stu_name: "Alex Chen",
      source_id: "source-student-1",
      source_filename: "S001-Alex-Chen.pdf",
      stu_ans: [{ q_id: "Q1", number: "1", content: "Answer one" }],
    },
  },
}));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({
    isLoading: false,
    isError: false,
    isSuccess: true,
    data: taskData,
    refetch: vi.fn(),
  }),
  useUpdateStudentAnswer: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateStudentIdentity: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

vi.mock("@/api/hooks/analytics", () => ({
  useAnalyticsFilterIntent: () => ({
    isPending: false,
    isError: false,
    error: null,
    mutate: filterIntentMocks.mutate,
    reset: filterIntentMocks.reset,
  }),
}));

vi.mock("@/hooks/useSourcePreview", () => ({
  useSourcePreview: () => ({
    descriptor: null,
    displayName: "S001-Alex-Chen.pdf",
    previewKind: "pdf",
    triggerState: "unavailable",
    unavailableReason: "missing",
    isOpen: false,
    loadState: "idle",
    errorCode: null,
    previewUrl: null,
    openPreview: sourcePreviewMocks.openPreview,
    closePreview: sourcePreviewMocks.closePreview,
    retryPreview: sourcePreviewMocks.retryPreview,
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US", t: (key: string) => key }),
}));

function recognizedIntent(overrides: Partial<FilterIntentResult> = {}): FilterIntentResult {
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
    question_types: [],
    max_average_confidence: null,
    missing_knowledge: false,
    min_max_score: null,
    max_max_score: null,
    preparation_status: null,
    material_field: null,
    material_status: null,
    submission_status: null,
    text_terms: [],
    explanation: "",
    ...overrides,
  };
}

function renderPage() {
  const router = createMemoryRouter([
    { path: "/tasks/:taskId/students/:studentId", element: <StudentAnswerReviewPage /> },
  ], { initialEntries: ["/tasks/task-1/students/S001"] });
  render(<RouterProvider router={router} />);
}

function respondWith(intent: FilterIntentResult) {
  filterIntentMocks.mutate.mockImplementation((_, options: { onSuccess?: (result: FilterIntentResult) => void }) => {
    window.setTimeout(() => options.onSuccess?.(intent), 0);
  });
}

describe("StudentAnswerReviewPage Ask SmarTAI", () => {
  beforeEach(() => {
    filterIntentMocks.mutate.mockReset();
    filterIntentMocks.reset.mockReset();
    sourcePreviewMocks.openPreview.mockReset();
    sourcePreviewMocks.closePreview.mockReset();
    sourcePreviewMocks.retryPreview.mockReset();
    Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => (
      { x: 0, y: 0, top: 0, right: 100, bottom: 100, left: 0, width: 100, height: 100, toJSON: () => ({}) }
    ));
  });

  it("filters an exact question locally without calling the model", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.type(input, "Q2");
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(document.getElementById("answer-question-Q2")).toBeInTheDocument());
    expect(document.getElementById("answer-question-Q1")).not.toBeInTheDocument();
    expect(filterIntentMocks.mutate).not.toHaveBeenCalled();
    expect(screen.getByText("Matched locally; no model call.")).toBeInTheDocument();
  });

  it("uses the model only to translate a free-form request into local controls", async () => {
    const user = userEvent.setup();
    respondWith(recognizedIntent({ submission_status: "missing" }));
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.type(input, "show the responses that still need attention");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(filterIntentMocks.mutate).toHaveBeenCalledTimes(1));
    expect(filterIntentMocks.mutate.mock.calls[0]?.[0]).toEqual({
      taskId: "task-1",
      question: "show the responses that still need attention",
      surface: "student_answer_review",
    });
    await waitFor(() => expect(document.getElementById("answer-question-Q2")).toBeInTheDocument());
    expect(document.getElementById("answer-question-Q1")).not.toBeInTheDocument();
  });

  it("keeps every question visible when the model returns an unsupported control", async () => {
    const user = userEvent.setup();
    respondWith(recognizedIntent({ sort: "name_asc" }));
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.type(input, "sort answers by an unsupported property");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(screen.getByText(
      "This student-answer view cannot apply that condition; no partial filter was applied.",
    )).toBeInTheDocument());
    expect(document.getElementById("answer-question-Q1")).toBeInTheDocument();
    expect(document.getElementById("answer-question-Q2")).toBeInTheDocument();
  });

  it("accepts a model-translated reverse question order for this headerless detail view", async () => {
    const user = userEvent.setup();
    respondWith(recognizedIntent({ sort: "question_desc" }));
    renderPage();
    const input = await screen.findByRole("textbox", { name: "Ask SmarTAI: filter this student's answers" });

    await user.type(input, "present the later-numbered prompt first");
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(filterIntentMocks.mutate).toHaveBeenCalledWith(
      {
        taskId: "task-1",
        question: "present the later-numbered prompt first",
        surface: "student_answer_review",
      },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    ));
    await waitFor(() => expect(
      Array.from(document.querySelectorAll("article[data-question-id]")).map((item) => item.getAttribute("data-question-id")),
    ).toEqual(["Q2", "Q1"]));
  });
});
