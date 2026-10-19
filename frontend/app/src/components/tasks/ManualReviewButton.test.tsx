import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ManualReviewButton } from "./ManualReviewButton";

const calls = vi.hoisted(() => ({ questions: vi.fn(), submissions: vi.fn(), grading: vi.fn() }));
vi.mock("@/api/tasks", () => ({ manuallyCompleteQuestionPreparation: calls.questions, manuallyCompleteSubmissionRecognition: calls.submissions, continueTaskRun: calls.grading }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN" }) }));
const task = { status: "error" as const, workflow_revision: 7, last_failed_job_id: "failed", grading_job_id: null, parse_job_id: null, extract_job_id: null };
function show(value: Parameters<typeof ManualReviewButton>[0]["task"], stage?: "questions" | "submissions" | "grading") {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(cache, "invalidateQueries");
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={["/recovery"]}><Routes>
    <Route path="/recovery" element={<ManualReviewButton taskId="T1" task={value} stage={stage} />} />
    <Route path="/tasks/T1/questions" element={<div>Question queue</div>} />
    <Route path="/tasks/T1/submissions" element={<div>Answer queue</div>} />
    <Route path="/tasks/T1/review" element={<div>Grade queue</div>} />
    <Route path="/tasks/T1/problems/progress" element={<div>Organizing saved questions</div>} />
    <Route path="/tasks/T1/submissions/progress" element={<div>Organizing saved answers</div>} />
  </Routes></MemoryRouter></QueryClientProvider>);
  return invalidate;
}
beforeEach(() => {
  Object.values(calls).forEach(call => call.mockReset().mockResolvedValue({ status: "ready_for_review", first_question_id: "Q1" }));
  calls.grading.mockResolvedValue({ status: "manual_review" });
});
describe("manual recovery entry", () => {
  it.each(["questions", "submissions"] as const)("uses existing progress while saved %s are already being organized", async stage => {
    calls[stage].mockResolvedValue({ status: "already_running", job_id: "saved-text" });
    show({ ...task, [stage === "questions" ? "extract_job_id" : "parse_job_id"]: "failed" });
    await userEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByText(stage === "questions" ? "Organizing saved questions" : "Organizing saved answers")).toBeVisible();
  });
  it.each([false, true])("hands off failed/paused questions without a model call (paused=%s)", async paused => {
    const invalidate = show({ ...task, status: paused ? "problems_paused" : "error", extract_job_id: "failed" });
    await userEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByText("Question queue")).toBeVisible();
    expect(calls.questions).toHaveBeenCalledWith({ taskId: "T1", jobId: "failed", expectedWorkflowRevision: 7 });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["tasks"] });
    expect(calls.grading).not.toHaveBeenCalled();
  });
  it.each([false, true])("hands off failed/paused submissions (paused=%s)", async paused => {
    show({ ...task, status: paused ? "submissions_paused" : "error", parse_job_id: "failed" });
    await userEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByText("Answer queue")).toBeVisible();
    expect(calls.submissions).toHaveBeenCalledWith({ taskId: "T1", jobId: "failed", expectedWorkflowRevision: 7 });
  });
  it.each([false, true])("hands off failed/paused grading with manual=true (paused=%s)", async paused => {
    show({ ...task, status: paused ? "grading_paused" : "error", grading_job_id: "failed" });
    await userEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByText("Grade queue")).toBeVisible();
    expect(calls.grading).toHaveBeenCalledWith("T1", "failed", 7, true);
  });
  it("does not expose stale or unrelated jobs as recoverable", () => {
    show({ ...task, extract_job_id: "old-job" });
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it("does not hand off another stage from an upload page", () => {
    show({ ...task, grading_job_id: "failed" }, "questions");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it("retains recovery on a rejected handoff and does not navigate", async () => {
    calls.submissions.mockRejectedValue(new Error("Task changed"));
    show({ ...task, parse_job_id: "failed" });
    await userEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Task changed"));
    expect(screen.queryByText("Answer queue")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "人工继续处理" })).toBeEnabled();
  });
});
