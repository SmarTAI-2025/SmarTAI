import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";

const state = vi.hoisted(() => ({
  data: {
    task_id: "task-1", status: "submissions_ready", workflow_revision: 3,
    problem_data: { Q1: { q_id: "Q1", number: "1", type: "short", stem: "Question" } },
    student_data: Object.fromEntries(["S001", "S002", "S003"].map((id) => [id, {
      stu_id: id, stu_name: `Student ${id}`, identity_status: "needs_review",
      stu_ans: [{ q_id: "Q1", content: "Original answer", flag: [], review_status: "pending" }],
    }])),
  },
  identity: vi.fn(), answer: vi.fn(), refetch: vi.fn(),
}));
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: state.data, isLoading: false, isSuccess: true, refetch: state.refetch }),
  useUpdateStudentIdentity: () => ({ mutateAsync: state.identity }),
  useUpdateStudentAnswer: () => ({ mutateAsync: state.answer }),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }) }));

function renderPage() {
  return render(<MemoryRouter initialEntries={["/tasks/task-1/submissions?q=S001"]}>
    <Routes><Route path="/tasks/:taskId/submissions" element={<SubmissionReviewOverviewPage />} /></Routes>
  </MemoryRouter>);
}
beforeEach(() => {
  state.identity.mockReset().mockImplementation(async ({ expectedWorkflowRevision }) => ({ workflow_revision: expectedWorkflowRevision + 1 }));
  state.answer.mockReset().mockImplementation(async ({ expectedWorkflowRevision }) => ({ workflow_revision: expectedWorkflowRevision + 1 }));
  state.refetch.mockReset().mockResolvedValue({ data: state.data });
});

describe("submission review confirmation", () => {
  it("links identity queue entries directly to the identity editor", () => {
    renderPage();
    const identityLink = screen.getAllByRole("link").find((link) => link.getAttribute("href")?.includes("identity=edit"));
    expect(identityLink).toHaveAttribute("href", "/tasks/task-1/students/S001?identity=edit&returnParams=q%3DS001");
  });

  it("confirms all identities with unchanged values and successive revisions, including filtered-out students", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "一键确认全部身份（3）" }));
    await waitFor(() => expect(state.identity).toHaveBeenCalledTimes(3));
    expect(state.identity.mock.calls.map(([input]) => [input.currentStudentId, input.studentId, input.studentName, input.expectedWorkflowRevision]))
      .toEqual([["S001", "S001", "Student S001", 3], ["S002", "S002", "Student S002", 4], ["S003", "S003", "Student S003", 5]]);
    expect(screen.getByText("已确认全部 3 位学生身份。")).toBeInTheDocument();
  });

  it("reports partial failure and links to the unfinished identity instead of claiming all confirmed", async () => {
    state.identity.mockResolvedValueOnce({ workflow_revision: 4 }).mockRejectedValueOnce(new Error("failed"));
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "一键确认全部身份（3）" }));
    await waitFor(() => expect(state.refetch).toHaveBeenCalledTimes(1));
    expect(state.identity).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/已确认 1 位学生身份，剩余 2 项未确认/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开未完成记录" })).toHaveAttribute("href", "/tasks/task-1/students/S002?identity=edit&returnParams=q%3DS001");
  });

  it("confirms answer review metadata without resending recognized text", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "全部确认" }));
    await waitFor(() => expect(state.answer).toHaveBeenCalledTimes(3));
    expect(state.answer.mock.calls.map(([input]) => input.expectedWorkflowRevision)).toEqual([3, 4, 5]);
    for (const [input] of state.answer.mock.calls) {
      expect(input.reviewStatus).toBe("confirmed");
      expect(input).not.toHaveProperty("content");
    }
  });
});
