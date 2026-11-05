import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { APIError } from "@/api/client";
import type { Task, TaskStateSnapshot, SubmissionSourceOutcome, JobProgress } from "@/types";
import { SubmissionRecognitionProgressPage } from "./SubmissionRecognitionProgressPage";

let detail: Partial<Task>;
let snapshot: Partial<TaskStateSnapshot> | undefined;
let progress: Partial<JobProgress> | null;
const manuallyComplete = vi.fn();
const retryRecognition = vi.fn();
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: detail, refetch: vi.fn() }),
  useRetrySubmissionRecognition: () => ({ isPending: false, mutateAsync: retryRecognition }),
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
    <Route path="/tasks/:taskId/submissions/upload" element={<h1>作答上传页面</h1>} />
  </Routes></MemoryRouter></I18nProvider>);
}
beforeEach(() => {
  window.localStorage.setItem("smartai_locale", "zh-CN");
  manuallyComplete.mockReset().mockResolvedValue({ status: "ready_for_review" });
  retryRecognition.mockReset().mockResolvedValue({ status: "started" });
  progress = null;
  detail = { task_id: "T", status: "parsing_submissions", parse_job_id: "new-job", workflow_revision: 1,
    submission_sources: [source("processing")],
    submission_source_summary: { uploaded: 1, parsed: 0, failed: 0, pending: 1, identity_needs_review: 0 } };
  snapshot = { ...detail, status: "submissions_ready", workflow_revision: 2,
    submission_sources: [source("identity_needs_review")],
    submission_source_summary: { uploaded: 1, parsed: 0, failed: 0, pending: 0, identity_needs_review: 1 } };
});

describe("submission recognition completion", () => {
  it("offers retry and manual review with retained results after the active work limit", async () => {
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: "submission_time_limit_reached",
      submission_sources: [{ ...source("failed"), reason_code: "submission_time_limit_reached" }],
      submission_source_summary: { uploaded: 1, parsed: 0, failed: 1, pending: 0, identity_needs_review: 0 } };
    show();
    expect(screen.getByRole("heading", { name: "本次作答识别已达到有效处理时限" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("有效处理上限为 2.5 小时");
    expect(screen.getByRole("alert")).toHaveTextContent("总时限为 3.5 小时");
    expect(screen.getByRole("alert")).toHaveTextContent("已保存的结果保留");
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeVisible();
  });

  it.each(["error", "submissions_paused"] as const)("shows an immediate handoff view during a slow %s request and restores recovery after rejection", async status => {
    snapshot = { ...detail, status, last_failed_job_id: "new-job", error: "operation_cancelled" };
    let reject!: (error: unknown) => void;
    manuallyComplete.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    show();
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(screen.queryByText("正在打开人工复核")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "正在打开…" })).toBeDisabled();
    if (status === "submissions_paused") expect(screen.getByRole("button", { name: "正在打开…" })).toHaveAttribute("aria-busy", "true");
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
  const archiveFailure = (reason = "submission_archive_nested_unsupported"): SubmissionSourceOutcome => ({
    ...source("failed"), internal_status: "parse_failed", file_name: "inner.zip",
    reason_code: reason, failure_phase: "archive", retryable: false, requires_reupload: true,
  });
  it.each(["submission_archive_nested_unsupported", "submission_archive_directories_only", "submission_archive_invalid"])("requires another upload for %s while retaining manual review", async reason => {
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: reason,
      submission_recognition_provider_id: "test-provider", submission_sources: [archiveFailure(reason)] };
    show();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "人工继续处理" })).toBeEnabled();
    fireEvent.click(screen.getByRole("link", { name: "返回修改配置" }));
    expect(await screen.findByRole("heading", { name: "作答上传页面" })).toBeVisible();
    expect(retryRecognition).not.toHaveBeenCalled();
  });
  it("does not retry healthy siblings when only the nested archive failed", async () => {
    snapshot = { ...detail, status: "submissions_ready", parse_job_id: "new-job",
      submission_sources: [source("parsed"), { ...archiveFailure(), source_id: "nested-source" }],
      submission_source_summary: { uploaded: 2, parsed: 1, failed: 1, pending: 0, identity_needs_review: 0 } };
    show();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeVisible();
  });
  it.each(["provider_timeout", "provider_auth_failed", "media_inspection_busy", "media_inspection_timeout",
    "media_inspection_unavailable", "media_inspection_failed", "media_inspection_resource_limit", "image_processing_failed"])("still retries mixed failures after recoverable %s", async reason => {
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: "submission_archive_nested_unsupported",
      submission_recognition_provider_id: "test-provider", submission_sources: [archiveFailure(), {
        ...source("failed"), source_id: "model-source", reason_code: reason, failure_phase: "recognition",
        retryable: reason === "provider_timeout",
      }] };
    show();
    fireEvent.click(screen.getByRole("button", { name: "重试失败项" }));
    expect(retryRecognition).toHaveBeenCalledOnce();
    expect(retryRecognition).toHaveBeenCalledWith(expect.objectContaining({ taskId: "T", jobId: "new-job" }));
  });
  it.each(["zh-CN", "en-US"])("handles the backend reupload guard with a stale snapshot in %s", async locale => {
    window.localStorage.setItem("smartai_locale", locale);
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: "submission_parse_failed",
      submission_recognition_provider_id: "test-provider" };
    retryRecognition.mockRejectedValueOnce(new APIError(409, "Repack and upload", {
      error: { code: "submission_retry_requires_reupload" },
    }));
    show();
    const retryLabel = locale === "zh-CN" ? "重试失败项" : "Retry failed items";
    fireEvent.click(screen.getByRole("button", { name: retryLabel }));
    expect(await screen.findByText(locale === "zh-CN" ? /剩余失败项需要重新整理文件/ : /The remaining failures require corrected files/)).toBeVisible();
    expect(screen.queryByRole("button", { name: retryLabel })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: locale === "zh-CN" ? "人工继续处理" : "Continue manually" })).toBeEnabled();
    expect(retryRecognition).toHaveBeenCalledOnce();
  });
  it("keeps pending sibling recovery available instead of inferring failure from the archive error alone", () => {
    snapshot = { ...detail, status: "submissions_paused", last_failed_job_id: "new-job", error: "operation_cancelled",
      submission_recognition_provider_id: "test-provider",
      submission_sources: [archiveFailure(), { ...source("processing"), source_id: "pending-source" }] };
    show();
    expect(screen.getByRole("button", { name: "继续识别" })).toBeEnabled();
  });
  it.each(["submission_source_empty", "submission_source_content_type_mismatch", "submission_source_unsupported",
    "image_mode_unsupported", "image_orientation_invalid", "image_multiframe_unsupported",
    "image_pixel_limit_exceeded", "image_input_too_large"])("requires reupload for an inner ZIP mixed with %s", async reason => {
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: "submission_archive_nested_unsupported",
      submission_recognition_provider_id: "test-provider", submission_sources: [
        { ...archiveFailure(), requires_reupload: true },
        { ...source("failed"), source_id: "invalid-file", reason_code: reason, failure_phase: "source_read", retryable: false, requires_reupload: true },
      ] };
    show();
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回修改配置" })).toHaveAttribute("href", "/tasks/T/submissions/upload");
    fireEvent.click(screen.getByRole("button", { name: "人工继续处理" }));
    expect(await screen.findByRole("heading", { name: "作答校对页面" })).toBeVisible();
    expect(retryRecognition).not.toHaveBeenCalled();
  });
  it("keeps uncertain mixed-batch retry behind explicit acknowledgement", () => {
    snapshot = { ...detail, status: "error", last_failed_job_id: "new-job", error: "provider_submit_uncertain",
      submission_recognition_provider_id: "test-provider", submission_sources: [archiveFailure(), {
        ...source("failed"), source_id: "uncertain-source", reason_code: "provider_submit_uncertain",
        failure_phase: "ocr", retryable: false, requires_reupload: false,
      }] };
    show();
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "重试失败项" }));
    expect(retryRecognition).toHaveBeenCalledWith(expect.objectContaining({ acknowledgePossibleDuplicateCall: true }));
  });
});
