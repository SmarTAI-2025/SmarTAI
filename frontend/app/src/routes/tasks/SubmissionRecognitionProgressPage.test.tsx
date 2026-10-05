import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { Task, TaskStateSnapshot, SubmissionSourceOutcome } from "@/types";
import { SubmissionRecognitionProgressPage } from "./SubmissionRecognitionProgressPage";

let detail: Partial<Task>;
let snapshot: Partial<TaskStateSnapshot> | undefined;
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: detail, refetch: vi.fn() }),
  useRetrySubmissionRecognition: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));
vi.mock("@/api/hooks", () => ({ useStageProviders: () => ({ data: [] }) }));
vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({ data: snapshot, progress: null, refetch: vi.fn() }),
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
  detail = { task_id: "T", status: "parsing_submissions", parse_job_id: "new-job", workflow_revision: 1,
    submission_sources: [source("processing")],
    submission_source_summary: { uploaded: 1, parsed: 0, failed: 0, pending: 1, identity_needs_review: 0 } };
  snapshot = { ...detail, status: "submissions_ready", workflow_revision: 2,
    submission_sources: [source("identity_needs_review")],
    submission_source_summary: { uploaded: 1, parsed: 0, failed: 0, pending: 0, identity_needs_review: 1 } };
});

describe("submission recognition completion", () => {
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
