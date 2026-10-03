import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddSubmissionsPage } from "./AddSubmissionsPage";

const mutateAsync = vi.fn();
const retryMutateAsync = vi.fn();
const providers = vi.hoisted(() => ({ enabled: true }));
const taskState = vi.hoisted(() => ({
  data: {
    task_id: "task-1",
    status: "problems_ready",
    workflow_revision: 0,
    grading_setup_configured: true,
    student_count: 0,
    submission_file_name: null as string | null,
    pending_submission_file_name: null as string | null,
    last_failed_job_id: null as string | null,
  },
}));

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({
    data: providers.enabled ? [{
      provider_id: "provider-default",
      provider_type: "openai",
      model: "gpt-test",
      enabled: true,
      is_default: true,
    }] : [],
    isError: false,
    isLoading: false,
  }),
  useTask: () => ({
    data: taskState.data,
    isError: false,
    isLoading: false,
  }),
  useParseSubmissions: () => ({
    isPending: false,
    mutateAsync,
  }),
  useRetrySubmissionRecognition: () => ({
    isPending: false,
    mutateAsync: retryMutateAsync,
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

function renderPage(taskId = "task-1") {
  return render(
    <MemoryRouter initialEntries={[`/tasks/${taskId}/submissions/upload`]}>
      <Routes>
        <Route path="/tasks/:taskId/submissions/upload" element={<AddSubmissionsPage />} />
        <Route path="/tasks/:taskId/submissions/progress" element={<div>progress page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AddSubmissionsPage OCR uploads", () => {
  beforeEach(() => {
    providers.enabled = true;
    mutateAsync.mockReset();
    retryMutateAsync.mockReset();
    mutateAsync.mockResolvedValue({ status: "started", task_id: "task-1" });
    retryMutateAsync.mockResolvedValue({ status: "started", task_id: "task-retry" });
    taskState.data = {
      task_id: "task-1",
      status: "problems_ready",
      workflow_revision: 0,
      grading_setup_configured: true,
      student_count: 0,
      submission_file_name: null,
      pending_submission_file_name: null,
      last_failed_job_id: null,
    };
  });
  it("focuses the missing file control instead of leaving the teacher at an unexplained error", () => {
    renderPage("missing-file-qa");
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(screen.getByRole("button", { name: "submissionUploadChoose" })).toHaveFocus();
    expect(screen.getAllByRole("alert").some((item) => item.textContent?.includes("submissionUploadFileRequired"))).toBe(true);
    expect(mutateAsync).not.toHaveBeenCalled();
  });
  it("links a missing model directly to BYOK while retaining the task return path", async () => {
    providers.enabled = false;
    renderPage("missing-model-qa");
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    const link = screen.getByRole("link", { name: "submissionUploadConfigureModels" });
    expect(link).toHaveAttribute("href", "/settings/byok?returnTo=%2Ftasks%2Fmissing-model-qa%2Fsubmissions%2Fupload");
    await waitFor(() => expect(link).toHaveFocus());
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("accepts a student image and sends it through the submission parsing mutation", async () => {
    const { container } = renderPage();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    expect(input.accept).toContain(".jpg");
    expect(input.accept).toContain(".jpeg");
    expect(input.accept).toContain(".png");
    expect(input.accept).toContain(".webp");

    const image = new File(["student answer"], "S003_Li_geography_notes.jpg", {
      type: "image/jpeg",
    });
    fireEvent.change(input, { target: { files: [image] } });
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
        taskId: "task-1",
        file: image,
        identityMode: "filename",
        recognitionProviderId: "provider-default",
      }));
    });
    expect(await screen.findByText("progress page")).toBeInTheDocument();
  });

  it("keeps the one-file-per-student upload contract visible after file selection", () => {
    const { container } = renderPage();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const submission = new File(["student answer"], "S003_Li.txt", {
      type: "text/plain",
    });

    fireEvent.change(input, { target: { files: [submission] } });

    expect(screen.getByText("submissionUploadFileContract")).toBeInTheDocument();
    expect(screen.getByText("S003_Li.txt")).toBeInTheDocument();
  });

  it("rejects empty submissions before starting recognition and accepts a replacement", async () => {
    const { container } = renderPage("empty-upload-test");
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File([], "empty.txt")] } });
    expect(screen.getByText(/所选作答文件为空/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(mutateAsync).not.toHaveBeenCalled();
    const valid = new File(["1. answer"], "student.txt");
    fireEvent.change(input, { target: { files: [valid] } });
    expect(screen.queryByText(/所选作答文件为空/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ file: valid })));
  });

  it("reuses a failed job's preserved original after the teacher switches models", async () => {
    taskState.data = {
      ...taskState.data,
      task_id: "task-retry",
      status: "error",
      workflow_revision: 7,
      pending_submission_file_name: "scan.pdf",
      last_failed_job_id: "job-failed",
    };
    renderPage("task-retry");

    expect(screen.getByText("scan.pdf")).toBeInTheDocument();
    expect(screen.getByText(/原文件已安全保留/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "用所选模型重试" }));

    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledWith({
      taskId: "task-retry",
      jobId: "job-failed",
      recognitionProviderId: "provider-default",
      expectedWorkflowRevision: 7,
    }));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(await screen.findByText("progress page")).toBeInTheDocument();
  });
});
