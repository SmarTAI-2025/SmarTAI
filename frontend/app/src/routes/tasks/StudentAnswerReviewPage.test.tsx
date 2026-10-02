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
const taskRefetch = vi.hoisted(() => vi.fn());
const mutations = vi.hoisted(() => ({ answer: vi.fn(), identity: vi.fn() }));
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
      source_choices: [] as Array<{ source_id: string; filename: string }>,
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
  useTask: () => ({ isLoading: false, isError: false, isSuccess: true, data: taskData, refetch: taskRefetch }),
  useUpdateStudentAnswer: () => ({ isPending: false, mutateAsync: mutations.answer }),
  useUpdateStudentIdentity: () => ({ isPending: false, mutateAsync: mutations.identity }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));

vi.mock("@/api/sourcePreview", () => ({
  getTaskSourceFiles: sourcePreviewApi.getTaskSourceFiles,
  isSourcePreviewCatalogMismatch: (error: unknown) => (
    typeof error === "object" && error !== null && "reason" in error
      && (error as { reason: unknown }).reason === "catalog_scope_mismatch"
  ),
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

function renderPage(search = "?question=Q1") {
  const router = createMemoryRouter([
    { path: "/tasks/:taskId/students/:studentId", element: <StudentAnswerReviewPage /> },
    { path: "/tasks/:taskId/submissions", element: <div>Submission overview</div> },
    { path: "/tasks/:taskId/grading-setup", element: <div>Grading setup</div> },
  ], { initialEntries: [`/tasks/task-1/students/S001${search}`] });
  render(<RouterProvider router={router} />);
}

beforeEach(() => {
  taskData.student_data.S001.identity_status = "matched";
  taskData.student_data.S001.stu_ans[0].review_status = "pending";
  mutations.answer.mockReset().mockResolvedValue({ workflow_revision: 4 });
  mutations.identity.mockReset().mockResolvedValue({ workflow_revision: 4, student: taskData.student_data.S001 });
  taskData.student_data.S001.source_choices = [];
  taskRefetch.mockReset().mockResolvedValue({ data: taskData });
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

describe("StudentAnswerReviewPage review shortcuts", () => {
  it("opens the editable identity form from the queue and confirms unchanged values", async () => {
    taskData.student_data.S001.identity_status = "needs_review";
    const user = userEvent.setup();
    renderPage("?identity=edit");
    expect(await screen.findByRole("textbox", { name: "学号" })).toHaveValue("S001");
    expect(screen.getByRole("textbox", { name: "姓名" })).toHaveValue("Lin");
    await user.click(screen.getByRole("button", { name: "确认身份已复核" }));
    expect(mutations.identity).toHaveBeenCalledWith({ taskId: "task-1", currentStudentId: "S001", studentId: "S001", studentName: "Lin", expectedWorkflowRevision: 3 });
  });

  it("confirms an identity from the student page without opening its editor", async () => {
    taskData.student_data.S001.identity_status = "needs_review";
    const user = userEvent.setup();
    renderPage();
    expect(screen.queryByRole("textbox", { name: "学号" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认身份已复核" }));
    expect(mutations.identity).toHaveBeenCalledTimes(1);
  });

  it("allows confirming recognized unflagged answers without editing their text", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "确认已复核" }));
    expect(mutations.answer).toHaveBeenCalledWith({ taskId: "task-1", studentId: "S001", qId: "Q1", expectedWorkflowRevision: 3, reviewStatus: "confirmed" });
  });

  it("keeps an unsaved answer draft and directs bulk review back to the edit", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "修改" }));
    const draft = document.querySelector("textarea")!;
    await user.type(draft, " corrected");
    await user.click(screen.getByRole("button", { name: "一键确认本学生全部作答" }));
    expect(mutations.answer).not.toHaveBeenCalled();
    expect(draft).toHaveValue("Student answer one corrected");
    expect(screen.getByRole("status")).toHaveTextContent("请先保存正在修改的作答");
    await user.click(screen.getByRole("button", { name: "保存并确认复核" }));
    expect(mutations.answer).toHaveBeenCalledWith(expect.objectContaining({ content: "Student answer one corrected", reviewStatus: "confirmed" }));
  });
});

describe("StudentAnswerReviewPage source preview", () => {
  it("selects an individual original from a multi-file student upload", async () => {
    taskData.student_data.S001.source_choices = [
      { source_id: "source-student-1", filename: "Page 1.pdf" },
      { source_id: "source-other", filename: "Page 2.pdf" },
    ];
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(await screen.findByRole("combobox"), "source-other");
    await user.click(screen.getByRole("button", { name: "查看原文件" }));
    await waitFor(() => expect(sourcePreviewApi.loadSourcePreviewFile).toHaveBeenCalledWith(
      "task-1", expect.objectContaining({ file_id: "file-other" }),
    ));
  });
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
    await waitFor(() => expect(sourcePreviewApi.getTaskSourceFiles).toHaveBeenCalledWith("task-1", 3));
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
