import "fake-indexeddb/auto";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { PageDraftSession } from "@/hooks/useDraftProtection";
import { clearPageDrafts, readPageDraft, objectDraftCodec } from "@/lib/pageDraftStore";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewOverviewPage } from "./ReviewOverviewPage";
import { ReviewDetailPage } from "./ReviewDetailPage";

const state = vi.hoisted(() => ({
  task: { task_id: "T1", name: "Review", status: "graded", workflow_revision: 5, problem_data: {}, student_data: {} },
  result: { results: [] as unknown[] } as { results: unknown[]; grading_run_status?: string },
  finalization: { remaining_review_count: 0, ready_for_confirmation: true, workflow_revision: 5 },
  bulk: vi.fn(), update: vi.fn(), finalize: vi.fn(), refetch: vi.fn(),
}));
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: state.task, refetch: state.refetch }),
  useTaskResult: () => ({ data: state.result, refetch: state.refetch }),
  useTeacherComments: () => ({ data: { comments: {} }, refetch: state.refetch }),
  useTaskFinalization: () => ({ data: state.finalization, refetch: state.refetch }),
  useConfirmTaskFinalization: () => ({ mutate: state.finalize }),
  useUpdateCorrectionReview: () => ({ mutateAsync: state.update }),
}));
vi.mock("@/hooks/useConfirmResultReviews", () => ({
  useConfirmResultReviews: () => ({ mutateAsync: state.bulk, progress: { completed: 1, total: 2 } }),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: ({ onLockedStepActivate }: { onLockedStepActivate: () => void }) => <button onClick={onLockedStepActivate}>Locked results</button>,
}));
vi.mock("@/components/tasks/AskQueryBar", () => ({ TaskQueryBar: () => null }));
vi.mock("@/hooks/useTaskFilterIntent", () => ({ useTaskFilterIntent: () => ({ intent: null, cancel: vi.fn() }) }));
vi.mock("@/components/tasks/ResultQuestionQuery", () => ({
  ResultQuestionQuery: () => null,
  useResultQuestionFilter: ({ questions }: { questions: unknown[] }) => ({ filter: {}, visibleQuestions: questions }),
}));
vi.mock("@/components/knowledge-base/KnowledgeCitationPreview", () => ({ KnowledgeCitationPreview: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US", t: (key: string) => key }) }));
function correction(qId: string, patch = {}) {
  return { q_id: qId, type: "calculation", score: 7, provisional_score: 7, max_score: 10, confidence: 0.8,
    comment: "AI note", steps: [], expert_results: [], requires_human_review: false, review_status: "confirmed", ...patch };
}
function show(detail = false, suffix = "") {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [
    { path: "/tasks/:taskId/review", element: <ReviewOverviewPage /> },
    { path: "/tasks/:taskId/review/:studentId/:questionId", element: <ReviewDetailPage /> },
  ] }], { initialEntries: [`/tasks/T1/review${detail ? "/S1/Q1" : suffix}`] });
  render(<PageDraftSession ownerId="review-teacher"><RouterProvider router={router} /></PageDraftSession>);
  return router;
}
beforeEach(async () => {
  await clearPageDrafts();
  vi.clearAllMocks();
  state.result.grading_run_status = undefined;
  state.result.results = [{ student_id: "S1", student_name: "Sample", corrections: [correction("Q1"), correction("Q2", { score: 0, provisional_score: 0, teacher_comment: "Keep this" })] }];
  state.finalization = { remaining_review_count: 0, ready_for_confirmation: true, workflow_revision: 5 };
  state.bulk.mockResolvedValue(7);
  state.update.mockImplementation(async (input) => ({ workflow_revision: input.expected_workflow_revision + 1,
    correction: correction(input.qId, { teacher_score: input.teacher_score, teacher_comment: input.teacher_comment }) }));
  vi.stubGlobal("scrollTo", vi.fn());
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
describe("one-click grading review", () => {
  it("provides a reachable partial failure retry while keeping the existing results visible", () => {
    state.result.grading_run_status = "partial_failed";
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1"), correction("Q2", { score: null, provisional_score: null, result_status: "failed" })] }];
    show();
    expect(screen.getByRole("link", { name: "Retry entire batch" })).toHaveAttribute("href", "/tasks/T1/grading/preflight");
    expect(screen.getByText(/Previous results are kept/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Some answers could not be graded" })).toBeInTheDocument();
  });
  it("shows the total confirmed count even when no AI result required review", () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { teacher_score: 7 }), correction("Q2", { teacher_score: 0 })] }];
    show();
    expect(screen.getByText("2/2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All reviews confirmed" })).toBeDisabled();
  });
  it("explicitly confirms automatically scored results including zero, without finalizing", async () => {
    show();
    await userEvent.click(screen.getByRole("button", { name: "Confirm all reviews (2)" }));
    expect(state.bulk).toHaveBeenCalledWith({ taskId: "T1", revision: 5, entries: [
      { studentId: "S1", qId: "Q1", score: 7, comment: "" },
      { studentId: "S1", qId: "Q2", score: 0, comment: "Keep this" },
    ] });
    expect(state.finalize).not.toHaveBeenCalled();
  });
  it("keeps teacher overrides and excludes an already confirmed teacher review", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { teacher_score: 9, review_status: "edited", teacher_comment: "Override" }), correction("Q2", { teacher_score: 7 })] }];
    show();
    await userEvent.click(screen.getByRole("button", { name: "Confirm all reviews (1)" }));
    expect(state.bulk.mock.calls[0][0].entries).toEqual([{ studentId: "S1", qId: "Q1", score: 9, comment: "Override" }]);
  });
  it("takes a blocked result directly to its missing score even with a no-match filter", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { score: null, provisional_score: null, result_status: "failed", synthesis_method: "all_failed", requires_human_review: true, review_reasons: ["llm_failed"] })] }];
    state.finalization = { remaining_review_count: 1, ready_for_confirmation: false, workflow_revision: 5 };
    const router = show(false, "?q=no-match");
    await userEvent.click(screen.getByRole("button", { name: "Locked results" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Go to the missing score" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks/T1/review/S1/Q1"));
    expect(state.bulk).not.toHaveBeenCalled();
  });
  it("confirms every student result in one action while chaining workflow revisions", async () => {
    show(true);
    await userEvent.click(screen.getByRole("button", { name: "Confirm all reviews for this student" }));
    await waitFor(() => expect(state.update).toHaveBeenCalledTimes(2));
    expect(state.update.mock.calls.map(([input]) => input.expected_workflow_revision)).toEqual([5, 6]);
    expect(state.update.mock.calls[1][0]).toMatchObject({ teacher_score: 0, teacher_comment: "Keep this" });
  });
  it("validates all drafts before saving and preserves the form on an invalid score", async () => {
    show(true);
    const inputs = screen.getAllByRole("textbox").filter((element) => element.tagName === "INPUT");
    await userEvent.clear(inputs[1]);
    await userEvent.type(inputs[1], "99");
    await userEvent.click(screen.getByRole("button", { name: "Confirm all reviews for this student" }));
    expect(state.update).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Correct the highlighted score first");
    expect(inputs[1]).toHaveValue("99");
  });
  it("stops after a failed item and reports a truthful partial confirmation", async () => {
    state.update.mockResolvedValueOnce({ workflow_revision: 6, correction: correction("Q1", { teacher_score: 7 }) }).mockRejectedValueOnce(new Error("Unavailable"));
    show(true);
    await userEvent.click(screen.getByRole("button", { name: "Confirm all reviews for this student" }));
    await waitFor(() => expect(screen.getAllByRole("alert").some((element) => element.textContent?.includes("Confirmed 1/2"))).toBe(true));
    expect(state.update).toHaveBeenCalledTimes(2);
  });
  it("confirms an unchanged teacher override that is still edited with one click", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", {
      teacher_score: 9, review_status: "edited", teacher_comment: "Keep my override",
    })] }];
    show(true);
    await userEvent.click(screen.getByRole("button", { name: /Q1.*Confirm review/ }));
    expect(state.update).toHaveBeenCalledExactlyOnceWith({
      taskId: "T1", studentId: "S1", qId: "Q1", expected_workflow_revision: 5,
      teacher_score: 9, teacher_comment: "Keep my override", confirm: true,
    });
  });
  it.each(["single", "student batch"])("keeps the current student's drafts isolated during %s saving", async (mode) => {
    state.result.results = [
      { student_id: "S1", student_name: "Sample", corrections: [correction("Q1"), correction("Q2", {
        score: 0, provisional_score: 0, requires_human_review: true, review_status: "pending", teacher_comment: "Keep this",
      })] },
      { student_id: "S2", student_name: "Other", corrections: [correction("Q1", { score: 2, teacher_comment: "Other student's note" })] },
    ];
    let completeFirst!: (value: unknown) => void;
    state.update.mockImplementationOnce(() => new Promise((resolve) => { completeFirst = resolve; }));
    const router = show(true);
    await userEvent.click(screen.getByRole("button", {
      name: mode === "single" ? /Q1.*Confirm/ : "Confirm all reviews for this student",
    }));
    expect(screen.getByText(/Saving review results\. Please wait before switching students/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Other" }));
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(router.state.location.pathname).toBe("/tasks/T1/review/S1/Q1");
    await act(async () => { await router.navigate("/tasks/T1/review/S2/Q1"); });
    expect(router.state.location.pathname).toBe("/tasks/T1/review/S1/Q1");
    expect(screen.getByRole("alertdialog")).toHaveTextContent("业务保存正在进行");
    expect(screen.getByRole("button", { name: "不暂存并离开" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "正在暂存…" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("7");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    await act(async () => {
      completeFirst({ workflow_revision: 6, correction: correction("Q1", { teacher_score: 7 }) });
    });
    await waitFor(() => expect(state.update).toHaveBeenCalledTimes(mode === "single" ? 1 : 2));
    expect(state.update.mock.calls.every(([input]) => input.studentId === "S1")).toBe(true);
    expect(router.state.location.pathname).toBe("/tasks/T1/review/S1/Q1");
    await waitFor(() => expect(screen.queryByText(/Saving review results\. Please wait before switching students/)).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("7");
  });
  it("stashes without confirming and continues the original destination only once", async () => {
    const router = show(true);
    const score = screen.getByRole("textbox", { name: "Q1 final score" });
    await userEvent.clear(score); await userEvent.type(score, "9");
    await userEvent.click(screen.getAllByRole("link", { name: "Back to Review Overview" })[0]);
    await userEvent.dblClick(screen.getByRole("button", { name: "暂存并离开" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks/T1/review"));
    expect(state.update).not.toHaveBeenCalled(); expect(state.finalize).not.toHaveBeenCalled(); expect(state.bulk).not.toHaveBeenCalled();
    await act(async () => { await router.navigate("/tasks/T1/review/S1/Q1"); });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("9"));
    expect(state.update).not.toHaveBeenCalled();
  });
  it.each([true, false])("formal confirmation clears only its corresponding draft on success=%s", async (success) => {
    show(true);
    const score = screen.getByRole("textbox", { name: "Q1 final score" });
    await userEvent.clear(score); await userEvent.type(score, "9");
    await userEvent.click(screen.getByRole("button", { name: "暂存" }));
    await screen.findByText(/已暂存 ·/);
    if (!success) state.update.mockRejectedValueOnce(new Error("Retry later"));
    await userEvent.click(screen.getByRole("button", { name: /Q1.*Save/ }));
    await waitFor(() => expect(state.update).toHaveBeenCalledTimes(1));
    const scope = "results-review:T1:S1:Q1";
    await waitFor(async () => {
      const loaded = await readPageDraft("review-teacher", scope, objectDraftCodec({ score: "", comment: "" }));
      if (success) expect(loaded.value).toBeNull();
      else { expect(loaded.value?.score).toBe("9"); expect(score).toHaveValue("9"); expect(screen.getAllByRole("alert").some(el=>el.textContent?.includes("Retry later"))).toBe(true); }
    });
  });
  it("loads fresh drafts when another task has the same student and question IDs", async () => {
    const router = show(true);
    expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("7");
    state.result = { results: [{ student_id: "S1", corrections: [correction("Q1", {
      score: 2, provisional_score: 2, teacher_comment: "Other task's note",
    })] }] };
    await act(async () => { await router.navigate("/tasks/T2/review/S1/Q1"); });
    expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("2");
    expect(screen.getByDisplayValue("Other task's note")).toBeInTheDocument();
    expect(state.update).not.toHaveBeenCalled();
  });
});
