import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SourceFileDescriptor } from "@/types/sourcePreview";
import { StudentAnswerReviewPage } from "./StudentAnswerReviewPage";

const sourcePreviewApi = vi.hoisted(() => ({
  getTaskSourceFiles: vi.fn(),
  loadSourcePreviewFile: vi.fn(),
}));
const studentSource: SourceFileDescriptor = {
  source_id: "source-student-1",
  file_id: "file-student-1",
  display_name: "S001-calculus.pdf",
  mime_type: "application/pdf",
  size_bytes: 43,
  status: "available",
  preview_kind: "pdf",
  unavailable_reason: null,
};
const taskData = vi.hoisted(() => ({
  task_id: "task-1",
  name: "Calculus Review",
  owner_id: "teacher-1",
  status: "submissions_ready",
  workflow_revision: 3,
  problem_count: 1,
  student_count: 1,
  kb_docs: {},
  kb_doc_count: 0,
  created_at: 1,
  updated_at: 1,
  problem_data: {
    Q1: {
      q_id: "Q1",
      number: "1",
      type: "Calculation",
      stem: "Evaluate the integral.",
      criterion: "Use a valid antiderivative.",
      max_score: 10,
      review_status: "confirmed",
    },
  },
  student_data: {
    S001: {
      stu_id: "S001",
      stu_name: "Lin",
      source_filename: "S001-calculus.pdf",
      source_id: "source-student-1",
      identity_status: "matched",
      identity_match_method: "filename",
      stu_ans: [{
        q_id: "Q1",
        number: "1",
        type: "Calculation",
        content: "Student answer one",
        flag: [],
        review_status: "pending",
      }],
    },
  },
}));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ isLoading: false, isError: false, isSuccess: true, data: taskData, refetch: vi.fn() }),
  useUpdateStudentAnswer: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateStudentIdentity: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));

vi.mock("@/api/sourcePreview", () => ({
  getTaskSourceFiles: sourcePreviewApi.getTaskSourceFiles,
  loadSourcePreviewFile: sourcePreviewApi.loadSourcePreviewFile,
  sourcePreviewErrorCode: () => "source_preview_load_failed",
}));

vi.mock("@/components/tasks/PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ url, title }: { url: string; title: string }) => (
    <object data={url} type="application/pdf" title={title} />
  ),
}));

vi.mock("@/i18n/I18nProvider", async () => {
  const { messages } = await vi.importActual<typeof import("@/i18n/messages")>("@/i18n/messages");
  return {
    useI18n: () => ({
      locale: "zh-CN",
      t: (key: keyof typeof messages["zh-CN"]) => messages["zh-CN"][key],
    }),
  };
});

function renderPage() {
  const router = createMemoryRouter([
    { path: "/tasks/:taskId/students/:studentId", element: <StudentAnswerReviewPage /> },
    { path: "/tasks/:taskId/submissions", element: <div>Submission overview</div> },
    { path: "/tasks/:taskId/grading-setup", element: <div>Grading setup</div> },
  ], { initialEntries: ["/tasks/task-1/students/S001?question=Q1"] });
  render(<RouterProvider router={router} />);
}

beforeEach(() => {
  sourcePreviewApi.getTaskSourceFiles.mockReset().mockResolvedValue({
    task_id: "task-1",
    workflow_revision: 3,
    problem_source: null,
    submission_sources: {
      "source-student-1": studentSource,
      "source-other": { ...studentSource, source_id: "source-other", file_id: "file-other" },
    },
  });
  sourcePreviewApi.loadSourcePreviewFile.mockReset().mockResolvedValue(
    new Blob(["pdf"], { type: "application/pdf" }),
  );
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:student-source") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    callback(0);
    return 1;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 100,
    bottom: 100,
    width: 100,
    height: 100,
    toJSON: () => ({}),
  });
});

describe("StudentAnswerReviewPage source preview", () => {
  it("maps the student by source_id and keeps an unsaved answer draft", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Student answer one")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "修改" }));
    const draft = document.querySelector("textarea");
    expect(draft).not.toBeNull();
    await user.clear(draft!);
    await user.type(draft!, "Unsaved corrected answer");

    const openButton = screen.getByRole("button", { name: "查看原文件" });
    await user.click(openButton);

    const panel = await screen.findByTestId("source-preview-panel");
    expect(screen.getByRole("separator", { name: "拖动调整原文件与识别内容宽度" })).toHaveAttribute("aria-valuenow", "50");
    await waitFor(() => expect(sourcePreviewApi.loadSourcePreviewFile).toHaveBeenCalledWith("task-1", studentSource));
    expect(await within(panel).findByTitle("原文件 · S001-calculus.pdf")).toHaveAttribute("data", "blob:student-source");
    expect(draft).toHaveValue("Unsaved corrected answer");

    await user.click(within(panel).getByRole("button", { name: "关闭对照" }));
    expect(screen.queryByTestId("source-preview-panel")).not.toBeInTheDocument();
    expect(draft).toHaveValue("Unsaved corrected answer");
    expect(openButton).toHaveFocus();
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:student-source"));
  });
});
