import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StudentAnswerReviewPage } from "./StudentAnswerReviewPage";

const taskData = vi.hoisted(() => ({
  task_id: "task-1",
  name: "SmarTAI Live Demo keyboard review",
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
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/components/tasks/PdfDocumentPreview", () => ({ PdfDocumentPreview: () => <div>Student PDF preview</div> }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US", t: (key: string) => key }) }));

beforeEach(() => {
  Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { callback(0); return 1; });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function getRect(this: HTMLElement) {
    const top = this.id === "answer-question-Q2" ? 600 : 0;
    return { x: 0, y: top, top, right: 100, bottom: top + 100, left: 0, width: 100, height: 100, toJSON: () => ({}) };
  });
});

describe("StudentAnswerReviewPage original-file keyboard navigation", () => {
  it("keeps preview arrows within the preview while preserving normal student navigation", async () => {
    const router = createMemoryRouter([
      { path: "/tasks/:taskId/students/:studentId", element: <StudentAnswerReviewPage /> },
    ], { initialEntries: ["/tasks/task-1/students/DEMO-001"] });
    render(<RouterProvider router={router} />);
    fireEvent.click(await screen.findByRole("button", { name: "sourcePreviewOpen" }));
    const preview = screen.getByTestId("source-preview-panel");
    const close = within(preview).getByRole("button", { name: "sourcePreviewClose" });
    expect(close).toHaveFocus();

    vi.mocked(window.scrollTo).mockClear();
    fireEvent.keyDown(close, { key: "ArrowRight" });
    fireEvent.keyDown(close, { key: "ArrowDown" });
    const separator = screen.getByRole("separator");
    fireEvent.keyDown(separator, { key: "ArrowDown" });
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(separator).toHaveAttribute("aria-valuenow", "52");
    expect(router.state.location.pathname).toBe("/tasks/task-1/students/DEMO-001");
    expect(window.scrollTo).not.toHaveBeenCalled();

    fireEvent.click(close);
    fireEvent.keyDown(window, { key: "ArrowRight" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks/task-1/students/DEMO-002"));
  });
});
