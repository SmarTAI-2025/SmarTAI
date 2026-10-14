import "fake-indexeddb/auto";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { PageDraftSession } from "@/hooks/useDraftProtection";
import { clearPageDrafts, readPageDraft, objectDraftCodec } from "@/lib/pageDraftStore";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewOverviewPage } from "./ReviewOverviewPage";
import { ReviewDetailPage } from "./ReviewDetailPage";

const state = vi.hoisted(() => ({
  locale: "en-US",
  task: { task_id: "T1", name: "Review", status: "graded", workflow_revision: 5, problem_data: {}, student_data: {}, grading_job_id: null as string | null, last_failed_job_id: null as string | null },
  result: { results: [] as unknown[] } as { results: unknown[]; grading_run_status?: string },
  finalization: { remaining_review_count: 0, ready_for_confirmation: true, workflow_revision: 5 },
  retry: vi.fn(), bulk: vi.fn(), update: vi.fn(), finalize: vi.fn(), refetch: vi.fn(),
}));
vi.mock("@/api/hooks/tasks", () => ({
  useStartGrading: () => ({ mutate: state.retry }),
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
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: state.locale, t: (key: string) => key }) }));
function correction(qId: string, patch = {}) {
  return { q_id: qId, type: "calculation", score: 7, provisional_score: 7, max_score: 10, confidence: 0.8,
    comment: "AI note", steps: [], expert_results: [], requires_human_review: false, review_status: "confirmed", ...patch };
}
function show(detail = false, suffix = "") {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [
    { path: "/tasks/:taskId/review", element: <ReviewOverviewPage /> },
    { path: "/tasks/:taskId/review/:studentId/:questionId", element: <ReviewDetailPage /> },
    { path: "/tasks/:taskId/grading/progress", element: <div>Grading recovery progress</div> },
  ] }], { initialEntries: [`/tasks/T1/review${detail ? "/S1/Q1" : suffix}`] });
  render(<PageDraftSession ownerId="review-teacher"><RouterProvider router={router} /></PageDraftSession>);
  return router;
}
beforeEach(async () => {
  state.locale = "en-US";
  await clearPageDrafts();
  state.task.status = "graded";
  state.task.grading_job_id = null;
  state.task.last_failed_job_id = null;
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
  it.each(["zh-CN", "en-US"])("keeps disputed AI evidence and matching teacher defaults visible in %s", async locale => {
    state.locale = locale;
    const experts = [7, 10].map((score, index) => ({ provider: `expert-${index}`, score, max_score: 10, confidence: index ? 0.99 : 0.95,
      comment: `Expert rationale ${index}`, steps: [{ step_no: 1, desc: `Expert reasoning ${index}`, score, is_correct: true }] }));
    state.result.results = [{ student_id: "S1", corrections: [
      correction("Q1", { score: 8.53, provisional_score: 8.53, expert_results: experts, comment: "Spread-only rationale" }),
      correction("Q2", { score: null, provisional_score: 8.53, expert_results: experts, comment: "Flagged rationale", requires_human_review: true,
        review_reasons: ["high_indecisiveness", "low_confidence"], review_status: "pending", confidence: 0.4,
        steps: [{ step_no: 1, desc: "Combined reasoning", score: 8.53, is_correct: true }] }),
    ] }];
    show(true);
    for (const id of ["Q1", "Q2"]) {
      const card = within(document.querySelector(`[data-question-id="${id}"]`) as HTMLElement);
      const result = card.getByRole("region", { name: locale === "zh-CN" ? "SmarTAI 批改结果" : "SmarTAI Grading Result" });
      expect(result).toHaveTextContent("8.5 / 10");
      expect(card.getByRole("textbox", { name: `${id} ${locale === "zh-CN" ? "最终得分" : "final score"}` })).toHaveValue("8.5");
      expect(result).toHaveTextContent(id === "Q1" ? "Spread-only rationale" : "Flagged rationale");
      await userEvent.click(card.getByText(locale === "zh-CN" ? "查看各专家原始结果" : "View Original Model Results"));
      expect(card.getByText("Expert rationale 0")).toBeVisible();
      expect(card.getByText("Expert reasoning 1")).toBeVisible();
    }
    expect(screen.getByText("Combined reasoning")).toBeVisible();
    expect(screen.queryByText(/已隐藏|details are hidden/)).not.toBeInTheDocument();
    expect(state.update).not.toHaveBeenCalled();
  });

  it("keeps original AI score after a teacher override and labels failed experts without a zero grade", () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", {
      score: 9, provisional_score: 8.53, teacher_score: 9,
      expert_results: [{ provider: "failed-model", score: 0, max_score: 10, confidence: 0, error_kind: "parse_failed", comment: "No parseable score", steps: [] }],
    })] }];
    show(true);
    expect(screen.getByRole("region", { name: "SmarTAI Grading Result" })).toHaveTextContent("8.5 / 10");
    expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("9");
    expect(screen.getByText(/failed-model/)).toHaveTextContent("No valid score · excluded from synthesis");
  });

  it("preserves a precise maximum in the default, fallback hint, and validation", () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", {
      score: 2.55, provisional_score: 2.55, max_score: 2.55,
    })] }];
    show(true);
    const input = screen.getByRole("textbox", { name: "Q1 final score" });
    expect(input).toHaveValue("2.55");
    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByText(/Leaving this blank/)).toHaveTextContent("AI score of 2.55.");
    fireEvent.change(input, { target: { value: "2.6" } });
    fireEvent.click(screen.getByRole("button", { name: "Q1 · Confirm review" }));
    expect(screen.getByText("Enter a score from 0 to 2.55.")).toBeInTheDocument();
    expect(state.update).not.toHaveBeenCalled();
  });

  it("opens failed-result details from recovery and keeps missing scores blocked", async () => {
    Object.assign(state.task, { status: "error", grading_job_id: "failed-run", last_failed_job_id: "failed-run" });
    state.result.grading_run_status = "partial_failed";
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1"), correction("Q2", {
      score: null, provisional_score: null, result_status: "failed", review_status: "pending",
    })] }];
    show(false, "?resolveFailures=1");
    await userEvent.click(screen.getByRole("link", { name: "View Grading Details" }));
    expect(screen.getByRole("textbox", { name: "Q2 final score" })).toBeInTheDocument();
    expect(screen.queryByText("Grading recovery progress")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "View Results & Analysis" }));
    expect(state.finalize).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });
  it("provides a reachable partial failure retry while keeping the existing results visible", async () => {
    state.result.grading_run_status = "partial_failed";
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1"), correction("Q2", { score: null, provisional_score: null, result_status: "failed" })] }];
    show();
    await userEvent.click(screen.getByRole("button", { name: "Retry failed items" }));
    expect(state.retry).toHaveBeenCalledWith(expect.objectContaining({taskId: "T1", retryScope: "failed_only", expectedWorkflowRevision: 5}), expect.anything());
    expect(screen.getByText(/Successful results and teacher scores/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Some answers could not be graded" })).toBeInTheDocument();
  });
  it("does not confirm a valid subset while another result is missing its score", async () => {
    state.result.results = [{student_id:"S1", corrections:[correction("Q1"), correction("Q2", {score:null, provisional_score:null, result_status:"failed"})]}];
    show();
    await userEvent.click(screen.getByRole("button", {name:"Confirm all"}));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(state.bulk).not.toHaveBeenCalled();
    expect(screen.getByRole("button", {name:"Retry failed items"})).toBeEnabled();
  });
  it("shows the total confirmed count even when no AI result required review", () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { teacher_score: 7 }), correction("Q2", { teacher_score: 0 })] }];
    show();
    expect(screen.getByText("2/2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmed" })).toBeDisabled();
  });
  it("requires confirmation of a repaired missing score without suggesting another model retry", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { score: 6, provisional_score: null, teacher_score: 6, review_status: "edited", confidence: 0.6 })] }];
    state.finalization = { remaining_review_count: 1, ready_for_confirmation: false, workflow_revision: 5 };
    show();
    expect(screen.getByText("Check the corrected score and confirm")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Locked results" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry failed items" })).not.toBeInTheDocument();
    expect(state.retry).not.toHaveBeenCalled();
  });
  it("keeps confirmation separate from the next stage when reopening completed results", async () => {
    state.task.status = "finalized";
    show();
    const actions = document.querySelector('[data-review-stage-actions]') as HTMLElement;
    expect(actions.querySelectorAll('button,a')).toHaveLength(2);
    await userEvent.click(within(screen.getByRole("region", { name: "Review queue" })).getByRole("button", { name: "Confirm all" }));
    expect(state.bulk).toHaveBeenCalledTimes(1);
    expect(state.finalize).not.toHaveBeenCalled();
  });
  it("explicitly confirms automatically scored results including zero, without finalizing", async () => {
    show();
    await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
    expect(state.bulk).toHaveBeenCalledWith({ taskId: "T1", revision: 5, entries: [
      { studentId: "S1", qId: "Q1", score: 7, comment: "" },
      { studentId: "S1", qId: "Q2", score: 0, comment: "Keep this" },
    ] });
    expect(state.finalize).not.toHaveBeenCalled();
  });
  it("keeps teacher overrides and excludes an already confirmed teacher review", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { teacher_score: 9, review_status: "edited", teacher_comment: "Override" }), correction("Q2", { teacher_score: 7 })] }];
    show();
    await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
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
  it.each([false, true])("keeps confirmation and results navigation separate in detail=%s", async detail => {
    show(detail);
    const actions = document.querySelector("[data-review-stage-actions]") as HTMLElement;
    expect(actions.children).toHaveLength(detail ? 3 : 2);
    if (detail) expect(actions.children[0]).toHaveTextContent("Confirm all");
    else expect(within(screen.getByRole("region", { name: "Review queue" })).getByRole("button", { name: "Confirm all" })).toBeVisible();
    expect(actions.children[detail ? 1 : 0]).toHaveTextContent(detail ? "Back to Review Overview" : "View Grading Details");
    expect(actions.children[detail ? 2 : 1]).toHaveTextContent("View Results & Analysis");
    Array.from(actions.children).forEach(button => expect(button).toHaveClass("h-10", "rounded-lg"));
    if (detail) {
      expect(within(document.querySelector('[data-question-id="Q1"]') as HTMLElement).getAllByRole("button", { name: /Q1.*Confirm review/ })).toHaveLength(1);
    }
    await userEvent.click(within(actions).getByRole("button", { name: "View Results & Analysis" }));
    expect(state.finalize).toHaveBeenCalledWith({ taskId: "T1", expectedWorkflowRevision: 5 }, expect.anything());
    expect(state.bulk).not.toHaveBeenCalled();
    expect(state.update).not.toHaveBeenCalled();
  });

  it("keeps a corrected failed score blocked until explicit review confirmation", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", { score: null, provisional_score: null, teacher_score: 7, review_status: "edited" })] }];
    state.finalization = { remaining_review_count: 1, ready_for_confirmation: false, workflow_revision: 5 };
    show();
    await userEvent.click(screen.getByRole("button", { name: "View Results & Analysis" }));
    expect(state.finalize).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Stay here" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
    expect(state.bulk).toHaveBeenCalledWith(expect.objectContaining({ entries: [{ studentId: "S1", qId: "Q1", score: 7, comment: "" }] }));
  });

  it("confirms every student result in one action while chaining workflow revisions", async () => {
    show(true);
    await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
    await waitFor(() => expect(state.update).toHaveBeenCalledTimes(2));
    expect(state.update.mock.calls.map(([input]) => input.expected_workflow_revision)).toEqual([5, 6]);
    expect(state.update.mock.calls[1][0]).toMatchObject({ teacher_score: 0, teacher_comment: "Keep this" });
  });
  it("validates all drafts before saving and preserves the form on an invalid score", async () => {
    show(true);
    const inputs = screen.getAllByRole("textbox").filter((element) => element.tagName === "INPUT");
    await userEvent.clear(inputs[1]);
    await userEvent.type(inputs[1], "99");
    await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
    expect(state.update).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Correct the highlighted score first");
    expect(inputs[1]).toHaveValue("99");
  });
  it("stops after a failed item and reports a truthful partial confirmation", async () => {
    state.update.mockResolvedValueOnce({ workflow_revision: 6, correction: correction("Q1", { teacher_score: 7 }) }).mockRejectedValueOnce(new Error("Unavailable"));
    show(true);
    await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
    await waitFor(() => expect(screen.getAllByRole("alert").some((element) => element.textContent?.includes("Confirmed 1/2"))).toBe(true));
    expect(state.update).toHaveBeenCalledTimes(2);
  });
  it("confirms an unchanged teacher override that is still edited with one click", async () => {
    state.result.results = [{ student_id: "S1", corrections: [correction("Q1", {
      teacher_score: 9, review_status: "edited", teacher_comment: "Keep my override",
    })] }];
    show(true);
    await userEvent.click(screen.getAllByRole("button", { name: /Q1.*Confirm review/ })[0]);
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
    await userEvent.click(screen.getAllByRole("button", {
      name: mode === "single" ? /Q1.*Confirm/ : "Confirm all",
    })[0]);
    expect(screen.getByText(/Saving review results\. Please wait before switching students/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Other" }));
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(router.state.location.pathname).toBe("/tasks/T1/review/S1/Q1");
    await act(async () => { await router.navigate("/tasks/T1/review/S2/Q1"); });
    expect(router.state.location.pathname).toBe("/tasks/T1/review/S1/Q1");
    expect(screen.getByRole("alertdialog")).toHaveTextContent("业务保存正在进行");
    expect(screen.getByRole("button", { name: "不暂存并离开" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "正在暂存…" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("7"));
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
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Q1 final score" })).toHaveValue("7"));
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
    await userEvent.click(screen.getAllByRole("button", { name: /Q1.*Confirm review/ })[0]);
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

it("shows ordinary grading as green and removes confirmed uncertainty from counts and queue", () => {
  state.result.results = [{ student_id: "S1", student_name: "Sample", corrections: [
    correction("Q1", { review_status: "pending", confidence: 0.9 }),
    correction("Q2", { review_status: "confirmed", teacher_score: 7, confidence: 0.3, requires_human_review: true }),
  ] }];
  show();
  expect(screen.getByRole("link", { name: /^Graded · Q1/ })).toHaveClass("bg-emerald-100");
  expect(screen.getByRole("link", { name: /^Confirmed · Q2/ })).toHaveClass("bg-emerald-100");
  expect(screen.getByText("Low-confidence responses").closest("div")).toHaveTextContent("0");
  expect(screen.queryByRole("link", { name: /Sample.*Q2/ })).not.toBeInTheDocument();
});


it("does not reopen a stale missing-score dialog after a successful batch", async () => {
  state.finalization = { remaining_review_count: 1, ready_for_confirmation: false, workflow_revision: 5 };
  show();
  await userEvent.click(screen.getByRole("button", { name: "Confirm all" }));
  await waitFor(() => expect(state.bulk).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});
