import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddSubmissionsPage } from "./AddSubmissionsPage";

const mutateAsync = vi.fn();
const retryMutateAsync = vi.fn();
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
  useExperts: () => ({
    data: [{
      provider_id: "provider-default",
      provider_type: "openai",
      model: "gpt-test",
      enabled: true,
      is_default: true,
    }],
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
