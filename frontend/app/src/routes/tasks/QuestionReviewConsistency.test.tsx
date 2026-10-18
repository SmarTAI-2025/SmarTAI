import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { useQuestionReview } from "@/hooks/useQuestionReview";
import { I18nProvider } from "@/i18n/I18nProvider";
import { taskKeys } from "@/api/hooks/keys";
import type { ProblemInfo, Task } from "@/types";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";
vi.mock("@/api/tasks", () => ({ getTask: vi.fn(), updateProblem: vi.fn(), updateStudentAnswer: vi.fn() }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
const api = await import("@/api/tasks");
beforeEach(() => { vi.clearAllMocks(); localStorage.setItem("smartai_locale", "zh-CN"); });
it.each(["zh-CN", "en-US"])("synchronizes cells, counts and queue after confirming a advisory package (%s)", async locale => {
  localStorage.setItem("smartai_locale", locale);
  const zh = locale === "zh-CN";
  const problem = { q_id: "Q1", number: "1", stem: "x + 1 = 2", type: "math", max_score: 10,
    reference_answer: "x = 1", criterion: "Solve", review_status: "needs_review", max_score_review_status: "confirmed",
    preparation_issues: [{ issue_id: "uncertain", code: "low_confidence", field: "stem", severity: "warning", status: "open", details: { confidence: .4 } }],
  } as ProblemInfo;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(taskKeys.detail("T"), { task_id: "T", status: "problems_ready", workflow_revision: 1, problem_data: { Q1: problem }, student_data: {} } as unknown as Task);
  // A slow refresh must not leave an acknowledged issue visible after a successful write.
  vi.mocked(api.getTask).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.updateProblem).mockResolvedValue({ status: "ok", q_id: "Q1", workflow_revision: 2,
    problem: { ...problem, review_status: "confirmed", preparation_issues: problem.preparation_issues!.map(issue => ({ ...issue, status: "acknowledged" })) } });
  render(<QueryClientProvider client={client}><I18nProvider><MemoryRouter initialEntries={["/tasks/T/questions"]}><Routes><Route path="/tasks/:taskId/questions" element={<QuestionPreparationOverviewPage />} /></Routes></MemoryRouter></I18nProvider></QueryClientProvider>);
  const queue = screen.getByRole("region", { name: zh ? "待复核队列" : "Review queue" });
  expect(within(queue).getAllByRole("link")).toHaveLength(1);
  expect(screen.getByRole("button", { name: zh ? "全部确认" : "Confirm all" })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: zh ? "全部确认" : "Confirm all" }));
  await waitFor(() => expect(within(queue).queryByRole("link")).not.toBeInTheDocument());
  expect(within(queue).queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getByText(zh ? "已识别" : "Recognized")).toHaveClass("bg-emerald-100");
  expect(screen.getAllByText(zh ? "状态正常" : "Ready").some(node => node.classList.contains("bg-emerald-100"))).toBe(true);
  expect(screen.getByText(zh ? /0 个复核项/ : /0 review items/)).toBeInTheDocument();
  expect(client.getQueryData<Task>(taskKeys.detail("T"))?.problem_data.Q1.preparation_issues?.[0].status).toBe("acknowledged");
  expect(api.updateProblem).toHaveBeenCalledWith("T", "Q1", { review_status: "confirmed", expected_workflow_revision: 1 });
  client.clear();
});

it("counts ordinary and flagged pending responses consistently, then clears both after confirmation", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const flagged = { q_id: "Q1", number: "1", type: "short", content: "literal student answer", flag: ["recognition_low_confidence"], review_status: "pending" as const };
  client.setQueryData(taskKeys.detail("T"), { task_id: "T", status: "submissions_ready", workflow_revision: 1,
    problem_data: { Q1: { q_id: "Q1", stem: "Question" } }, student_data: {
      S1: { stu_id: "S1", stu_name: "Normal", identity_status: "matched", stu_ans: [{ ...flagged, flag: ["external_annotation_present"] }] },
      S2: { stu_id: "S2", stu_name: "Uncertain", identity_status: "matched", stu_ans: [flagged] },
    },
  } as unknown as Task);
  vi.mocked(api.getTask).mockImplementation(() => new Promise(() => {}));
  vi.mocked(api.updateStudentAnswer).mockImplementation(async (_task, student) => ({ status: "ok", stu_id: student,
    q_id: "Q1", workflow_revision: student === "S1" ? 2 : 3, answer: { ...flagged, review_status: "confirmed" } }));
  render(<QueryClientProvider client={client}><I18nProvider><MemoryRouter initialEntries={["/tasks/T/submissions"]}><Routes><Route path="/tasks/:taskId/submissions" element={<SubmissionReviewOverviewPage />} /></Routes></MemoryRouter></I18nProvider></QueryClientProvider>);
  const queue = screen.getByRole("region", { name: "待复核队列" });
  expect(within(queue).getAllByRole("link")).toHaveLength(2);
  expect(screen.getByText("待确认题次").parentElement).toHaveTextContent("2");
  expect(screen.getAllByRole("link", { name: /^待复核/ })).toHaveLength(2);
  expect(screen.getAllByRole("link", { name: /^待复核/ }).every(link => link.classList.contains("bg-amber-100"))).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "全部确认" }));
  await waitFor(() => expect(within(queue).queryByRole("link")).not.toBeInTheDocument());
  expect(within(queue).queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "已确认" })).toHaveLength(2);
  expect(screen.getByText("待确认题次").parentElement).toHaveTextContent("0");
  expect(vi.mocked(api.updateStudentAnswer).mock.calls.map(call => call[3].expected_workflow_revision)).toEqual([1, 2]);
  client.clear();
});


it("can explicitly confirm a saved rubric while its manual-completion marker still exists", async () => {
  const problem = { q_id: "Q1", number: "1", type: "short_answer", stem: "Visible question", criterion: "Teacher completed rubric", reference_answer: "", max_score: 10,
    preparation_issues: [{ issue_id: "manual", code: "manual_completion_required", field: "source", severity: "warning", status: "open", details: { required_fields: ["stem", "criterion"] } }],
  } as ProblemInfo;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(api.updateProblem).mockResolvedValue({ status: "ok", q_id: "Q1", workflow_revision: 2, problem: { ...problem, review_status: "confirmed" } });
  const { result } = renderHook(() => useQuestionReview("T", 1), { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
  await act(async () => { expect(await result.current.confirm([problem])).toBe(true); });
  expect(api.updateProblem).toHaveBeenCalledWith("T", "Q1", { review_status: "confirmed", expected_workflow_revision: 1 });
  expect(result.current.blocked).toEqual([]);
  client.clear();
});
