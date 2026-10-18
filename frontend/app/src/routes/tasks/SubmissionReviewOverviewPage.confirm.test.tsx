import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";

const state = vi.hoisted(() => ({
  data: {
    task_id: "task-1", status: "submissions_ready", workflow_revision: 3,
    parse_job_id: "manual-job-1", manual_student_count_required: false,
    problem_data: { Q1: { q_id: "Q1", number: "1", type: "short", stem: "Question" } },
    student_data: Object.fromEntries(["S001", "S002", "S003"].map((id) => [id, {
      stu_id: id, stu_name: `Student ${id}`, source_id: `source-${id}`, identity_status: "needs_review",
      stu_ans: [{ q_id: "Q1", content: "Original answer", flag: [] as string[], review_status: "pending" }],
    }])),
  },
  identity: vi.fn(), answer: vi.fn(), manual: vi.fn(), add: vi.fn(), remove: vi.fn(), refetch: vi.fn(),
}));
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: state.data, isLoading: false, isSuccess: true, refetch: state.refetch }),
  useUpdateStudentIdentity: () => ({ mutateAsync: state.identity }),
  useAddTaskStudent: () => ({ mutateAsync: state.add, isPending: false }),
  useDeleteTaskStudent: () => ({ mutateAsync: state.remove, isPending: false }),
  useManuallyCompleteSubmissionRecognition: () => ({ mutateAsync: state.manual, isPending: false }),
  useUpdateStudentAnswer: () => ({ mutateAsync: state.answer }),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }) }));

function renderPage(query = "") {
  return render(<MemoryRouter initialEntries={[`/tasks/task-1/submissions${query}`]}>
    <Routes><Route path="/tasks/:taskId/submissions" element={<SubmissionReviewOverviewPage />} /></Routes>
  </MemoryRouter>);
}
beforeEach(() => {
  state.data.status = "submissions_ready";
  state.data.manual_student_count_required = false;
  state.manual.mockReset().mockResolvedValue({ status: "ready_for_review" });
  state.add.mockReset().mockResolvedValue({ status: "ok", student_id: "manual-1", workflow_revision: 4 });
  state.remove.mockReset().mockResolvedValue({ status: "ok", workflow_revision: 4 });
  for (const student of Object.values(state.data.student_data)) {
    student.identity_status = "needs_review";
    student.stu_ans[0].flag = [];
  }
  state.identity.mockReset().mockImplementation(async ({ expectedWorkflowRevision }) => ({ workflow_revision: expectedWorkflowRevision + 1 }));
  state.answer.mockReset().mockImplementation(async ({ expectedWorkflowRevision }) => ({ workflow_revision: expectedWorkflowRevision + 1 }));
  state.refetch.mockReset().mockResolvedValue({ data: state.data });
});

describe("submission review confirmation", () => {
  it("links identity queue entries directly to the identity editor", () => {
    renderPage();
    const identityLink = screen.getAllByRole("link").find((link) => link.getAttribute("href")?.includes("identity=edit"));
    expect(identityLink).toHaveAttribute("href", "/tasks/task-1/students/S001?identity=edit");
  });

  it.each(["submissions_ready", "grading_setup"])("does not offer bulk identity confirmation from %s", status => {
    state.data.status = status;
    renderPage();
    expect(screen.getByRole("tab", { name: "需要处理 (3)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: "全部确认" })).not.toBeInTheDocument();
    expect(state.identity).not.toHaveBeenCalled();
  });

  function suggestAnswers() {
    for (const student of Object.values(state.data.student_data)) {
      student.identity_status = "matched";
      student.stu_ans[0].flag = ["recognition_low_confidence"];
    }
  }

  it("confirms all visible suggestions, without resending content or identities", async () => {
    suggestAnswers();
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "全部确认" }));
    await waitFor(() => expect(state.answer).toHaveBeenCalledTimes(3));
    expect(state.answer).toHaveBeenCalledWith({ taskId: "task-1", studentId: "S001", qId: "Q1", reviewStatus: "confirmed", expectedWorkflowRevision: 3 });
    expect(state.identity).not.toHaveBeenCalled();
    expect(screen.getByText("已确认全部 3 项。")).toBeInTheDocument();
  });

  it("reports partial failure and links to the unfinished answer", async () => {
    suggestAnswers();
    state.answer.mockResolvedValueOnce({ workflow_revision: 4 }).mockRejectedValueOnce(new Error("failed"));
    const user = userEvent.setup();
    renderPage("");
    await user.click(screen.getByRole("button", { name: "全部确认" }));
    await waitFor(() => expect(state.refetch).toHaveBeenCalledTimes(1));
    expect(state.answer).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/已确认 1 项，剩余 2 项未确认/)).toBeInTheDocument();
    expect(state.identity).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "打开未完成记录" })).toHaveAttribute("href", "/tasks/task-1/students/S002?question=Q1");
  });

  it("confirms suggestions while leaving required items untouched", async () => {
    suggestAnswers();
    state.data.student_data.S002.stu_ans[0].flag = ["recognition_manual_pending"];
    const user = userEvent.setup();
    renderPage("");
    expect(screen.queryByRole("button", { name: "全部确认" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "建议处理 (2)" }));
    await user.click(screen.getByRole("button", { name: "全部确认" }));
    await waitFor(() => expect(state.answer).toHaveBeenCalledTimes(2));
    expect(state.answer.mock.calls.map(([input]) => [input.studentId, input.expectedWorkflowRevision])).toEqual([["S001", 3], ["S003", 4]]);
    for (const [input] of state.answer.mock.calls) expect(input).not.toHaveProperty("content");
  });
});


it("creates unknown student rows inside the original review matrix with the current job and revision", async () => {
  const originalStudents = state.data.student_data;
  state.data.student_data = {};
  state.data.manual_student_count_required = true;
  try {
    const user = userEvent.setup();
    renderPage();
    expect(screen.getByRole("heading", { name: "submissionReviewTitle", level: 1 })).toBeInTheDocument();
    const count = screen.getByRole("spinbutton", { name: "学生人数" });
    const create = screen.getByRole("button", { name: "建立学生行" });
    expect(create).toBeDisabled();
    await user.type(count, "501");
    expect(create).toBeDisabled();
    await user.clear(count);
    await user.type(count, "2");
    await user.click(create);
    await waitFor(() => expect(state.manual).toHaveBeenCalledWith({ taskId: "task-1", jobId: "manual-job-1", expectedWorkflowRevision: 3, studentCount: 2 }));
    expect(screen.queryByText("submissionReviewUploadAgain")).not.toBeInTheDocument();
  } finally {
    state.data.student_data = originalStudents;
  }
});

it("keeps a rejected student-count save on the original matrix without hiding the error", async () => {
  const originalStudents = state.data.student_data;
  state.data.student_data = {};
  state.data.manual_student_count_required = true;
  state.manual.mockRejectedValueOnce(new Error("stale revision"));
  try {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByRole("spinbutton", { name: "学生人数" }), "2");
    await user.click(screen.getByRole("button", { name: "建立学生行" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("建立学生行失败");
    expect(screen.getByRole("spinbutton", { name: "学生人数" })).toHaveValue(2);
  } finally {
    state.data.student_data = originalStudents;
  }
});


it("adds a blank student from the action row beside the matrix count", async () => {
  renderPage();
  const add = screen.getByRole("button", { name: "添加学生" });
  expect(add).toHaveClass("h-10");
  expect(add.parentElement).toHaveTextContent("显示 3/3 位学生");
  await userEvent.click(add);
  await waitFor(() => expect(state.add).toHaveBeenCalledWith({ taskId: "task-1", expectedWorkflowRevision: 3 }));
  expect(state.refetch).toHaveBeenCalled();
});

it("requires confirmation before deleting one student through the original matrix", async () => {
  renderPage();
  const remove = screen.getByRole("button", { name: "删除学生 S001" });
  expect(remove).toHaveAttribute("title", "删除");
  expect(remove.parentElement?.querySelector("a")).toHaveAttribute("title", "查看");
  await userEvent.click(remove);
  expect(state.remove).not.toHaveBeenCalled();
  expect(screen.getByRole("alertdialog")).toHaveTextContent("Student S001");
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(state.remove).not.toHaveBeenCalled();
  await userEvent.click(remove);
  await userEvent.click(screen.getByRole("button", { name: "删除" }));
  await waitFor(() => expect(state.remove).toHaveBeenCalledWith({ taskId: "task-1", studentId: "S001", expectedWorkflowRevision: 3 }));
});

it("keeps adding students available when the review matrix has no students", async () => {
  const originalStudents = state.data.student_data;
  state.data.student_data = {};
  try {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "添加学生" }));
    expect(state.add).toHaveBeenCalledWith({ taskId: "task-1", expectedWorkflowRevision: 3 });
    expect(screen.queryByRole("link", { name: "submissionReviewUploadAgain" })).not.toBeInTheDocument();
  } finally {
    state.data.student_data = originalStudents;
  }
});

it("does not expose row mutation controls when viewing completed grading", () => {
  state.data.status = "graded";
  renderPage();
  expect(screen.queryByRole("button", { name: "添加学生" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^删除学生/ })).not.toBeInTheDocument();
});


it("clears the matrix filter after adding so the new blank student is visible", async () => {
  renderPage("?q=S001&sort=name_desc");
  const query = screen.getByRole("textbox", { name: "Ask SmarTAI：学生作答" });
  expect(query).toHaveValue("S001");
  await userEvent.click(screen.getByRole("button", { name: "添加学生" }));
  await waitFor(() => expect(state.add).toHaveBeenCalledWith({ taskId: "task-1", expectedWorkflowRevision: 3 }));
  await waitFor(() => expect(query).toHaveValue(""));
});
