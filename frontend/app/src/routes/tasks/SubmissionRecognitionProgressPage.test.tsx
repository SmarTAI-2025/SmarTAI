import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { Task, TaskStateSnapshot, SubmissionSourceOutcome, JobProgress } from "@/types";
import { SubmissionRecognitionProgressPage } from "./SubmissionRecognitionProgressPage";

let detail: Partial<Task>;
let snapshot: Partial<TaskStateSnapshot> | undefined;
let progress: Partial<JobProgress> | null;
const manuallyComplete = vi.fn();
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: detail, refetch: vi.fn() }),
  useRetrySubmissionRecognition: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useManuallyCompleteSubmissionRecognition: () => ({ isPending: false, mutateAsync: manuallyComplete }),
}));
vi.mock("@/api/hooks", () => ({ useStageProviders: () => ({ data: [] }) }));
vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({ data: snapshot, progress, refetch: vi.fn() }),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));

const source = (status: SubmissionSourceOutcome["status"]): SubmissionSourceOutcome => ({
  source_id: "synthetic-source", file_id: "synthetic-file", file_name: "synthetic.txt",
  content_type: "text/plain", size_bytes: 20, status, internal_status: "parsed",
  retryable: status === "failed", matched_answer_count: 1, unknown_question_ids: [],
  job_id: "new-job", attempt: 1, created_at: 1,
});
function show() {
  render(<I18nProvider><MemoryRouter initialEntries={["/tasks/T/submissions/progress"]}><Routes>
    <Route path="/tasks/:taskId/submissions/progress" element={<SubmissionRecognitionProgressPage />} />
    <Route path="/tasks/:taskId/submissions" element={<h1>作答校对页面</h1>} />
  </Routes></MemoryRouter></I18nProvider>);
}
beforeEach(() => {
  manuallyComplete.mockReset().mockResolvedValue({ status: "ready_for_review" });
  progress = null;
  detail = { task_id: "T", status: "parsing_submissions", parse_job_id: "new-job", workflow_revision: 1,
    submission_sources: [source("processing")],
    submission_source_summary: { uploaded: 1, parsed: 0, failed: 0, pending: 1, identity_needs_review: 0 } };
  snapshot = { ...detail, status: "submissions_ready", workflow_revision: 2,
    submission_sources: [source("identity_needs_review")],
    submission_source_summary: { uploaded: 1, parsed: 0, failed: 0, pending: 0, identity_needs_review: 1 } };
});

describe("submission recognition completion", () => {
  it.each(["error", "submissions_paused"] as const)("shows an immediate handoff view during a slow %s request and restores recovery after rejection", async status => {
    snapshot = { ...detail, status, last_failed_job_id: "new-job", error: "operation_cancelled" };
    let reject!: (error: unknown) => void;
    manuallyComplete.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    show();
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(screen.getByText("正在打开人工复核")).toBeVisible();
    expect(screen.getByRole("button", { name: "正在打开…" })).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "作答校对页面" })).not.toBeInTheDocument();
    reject(new Error("Recovery request failed"));
    expect(await screen.findByRole("button", { name: "人工继续处理" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeVisible();
    expect(manuallyComplete).toHaveBeenCalledTimes(2);
  });
  it.each(["started", "already_running"])("rejects a retired %s handoff instead of entering implicit AI recovery", async responseStatus => {
    snapshot = { ...detail, status: "submissions_paused", last_failed_job_id: "new-job", error: "operation_cancelled" };
    manuallyComplete.mockResolvedValue({ status: responseStatus });
    const view = () => <I18nProvider><MemoryRouter initialEntries={["/tasks/T/submissions/progress"]}><Routes>
      <Route path="/tasks/:taskId/submissions/progress" element={<SubmissionRecognitionProgressPage />} />
      <Route path="/tasks/:taskId/submissions" element={<h1>作答校对页面</h1>} />
    </Routes></MemoryRouter></I18nProvider>;
    render(view());
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByRole("heading", { name: "此旧整理任务已停用" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "作答校对页面" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
  });
  it("keeps direct visits to retired jobs out of both retry and retired manual recovery", async () => {
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: "saved_text_recovery_retired" };
    show();
    expect(screen.getByRole("heading", { name: "此旧整理任务已停用" })).toBeVisible();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
    expect(manuallyComplete).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "人工继续处理" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "新建任务" })).toHaveAttribute("href", "/tasks/new");
  });
  it("opens manual review from a paused task without a model selection", async () => {
    snapshot = { ...detail, status: "submissions_paused", last_failed_job_id: "new-job", error: "operation_cancelled" };
    show();
    expect(manuallyComplete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeInTheDocument();
    expect(manuallyComplete).toHaveBeenCalledWith({ taskId: "T", jobId: "new-job", expectedWorkflowRevision: 1 });
  });
  it("shows a recorded provider 503 while keeping retry confirmation", () => {
    snapshot!.status = "error";
    snapshot!.error = "provider_submit_uncertain";
    snapshot!.last_failed_job_id = "new-job";
    progress = { provider_failures: [{ code: "provider_upstream_unavailable", http_status: 503 }] };
    show();
    expect(screen.getByRole("heading", { name: "模型服务暂时不可用" })).toBeInTheDocument();
    expect(screen.getByText(/HTTP 503/)).toHaveTextContent("计费尚无法确认");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeDisabled();
  });

  it.each(["processing", "failed"] as const)("opens identity review despite stale %s detail", async (status) => {
    detail.submission_sources = [source(status)];
    show();
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
  });
  it("opens review when recognition succeeded", async () => {
    snapshot!.submission_sources = [source("parsed")];
    snapshot!.submission_source_summary = { uploaded: 1, parsed: 1, failed: 0, pending: 0, identity_needs_review: 0 };
    show();
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeInTheDocument();
  });
  it("keeps real failed files on recovery despite previously successful detail", () => {
    detail = { ...snapshot };
    snapshot!.submission_sources = [source("failed")];
    snapshot!.submission_source_summary = { uploaded: 1, parsed: 0, failed: 1, pending: 0, identity_needs_review: 0 };
    show();
    expect(screen.getByRole("heading", { name: "作答识别未完成" })).toBeInTheDocument();
    expect(screen.queryByText("作答校对页面")).not.toBeInTheDocument();
  });
  it("does not hide a terminal error when every file was read", () => {
    snapshot!.status = "error";
    snapshot!.error = "provider_timeout";
    show();
    expect(screen.getByRole("heading", { name: "作答识别未完成" })).toBeInTheDocument();
  });
  it("uses completed detail before the first progress snapshot arrives", async () => {
    detail = { ...snapshot }; snapshot = undefined;
    show();
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeInTheDocument();
  });
});
