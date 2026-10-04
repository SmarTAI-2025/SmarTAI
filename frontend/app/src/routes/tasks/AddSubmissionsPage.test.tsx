import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, Outlet } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageDraftSession } from "@/hooks/usePageDraft";
import { clearPageDrafts } from "@/lib/pageDraftStore";
import { APIError } from "@/api/client";
import { rememberImageReturn } from "@/lib/imageRecoveryNavigation";
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

const inputState = vi.hoisted(() => ({ input: null as unknown }));
vi.mock("@/api/workflowInputs", () => ({ useWorkflowInput: () => ({ data: { input: inputState.input }, isError: false, isLoading: false }) }));
vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({
    data: providers.enabled ? [{
      provider_id: "provider-default",
      provider_type: "openai",
      model: "gpt-test",
      enabled: true,
      is_default: true,
    }, { provider_id: "provider-new", provider_type: "moonshot", model: "arbitrary-new-model", enabled: true }] : [],
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

function renderPage(taskId = "task-1", owner = "submission-teacher") {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [
    { path: "/tasks/:taskId/submissions/upload", element: <AddSubmissionsPage /> },
    { path: "/tasks/:taskId/submissions/progress", element: <div>progress page</div> },
    { path: "/settings/byok", element: <div>BYOK</div> },
  ] }], { initialEntries: [`/tasks/${taskId}/submissions/upload`] });
  return { ...render(<PageDraftSession ownerId={owner}><RouterProvider router={router} /></PageDraftSession>), router };
}

describe("AddSubmissionsPage OCR uploads", () => {
  beforeEach(async () => {
  inputState.input = null;
    vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File);
    await clearPageDrafts();

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

  it("saves both answer and roster bytes before recovery and keeps the new model on browser back", async () => {
    const { container, router } = renderPage();
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["answer bytes"], "S003_Li.png", { type: "image/png" })] } });
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    const inputs = container.querySelectorAll('input[type="file"]');
    fireEvent.change(inputs[1]!, { target: { files: [new File(["student_id,name\nS003,Li"], "roster.csv", { type: "text/csv" })] } });
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    mutateAsync.mockRejectedValueOnce(new APIError(422, "image_recognition_unconfirmed", { detail: { code: "image_recognition_unconfirmed" } }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    fireEvent.click(await screen.findByRole("button", { name: "暂存并去验证" }));
    await screen.findByText("BYOK");
    expect(router.state.location.search).toContain("returnTo=%2Ftasks%2Ftask-1%2Fsubmissions%2Fupload");
    rememberImageReturn("submission-teacher", "/tasks/task-1/submissions/upload", "provider-new");
    await act(async () => { await router.navigate(-1); });
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-new"));
    expect(screen.getByText("S003_Li.png")).toBeInTheDocument();
    expect(screen.getByText("roster.csv")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" })).toHaveAttribute("aria-checked", "true");
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(2));
    const request = mutateAsync.mock.calls[1][0];
    expect(request.recognitionProviderId).toBe("provider-new");
    expect(await request.file.text()).toBe("answer bytes");
    expect(await request.rosterFile.text()).toBe("student_id,name\nS003,Li");
  });
  it("focuses the missing file control instead of leaving the teacher at an unexplained error", async () => {
    renderPage("missing-file-qa");
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
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
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
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
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(mutateAsync).not.toHaveBeenCalled();
    const valid = new File(["1. answer"], "student.txt");
    fireEvent.change(input, { target: { files: [valid] } });
    expect(screen.queryByText(/所选作答文件为空/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
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
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("button", { name: "按当前配置重试" }));

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


it("keeps unsubmitted local files through task navigation but clears after a normal start", async () => {
  vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File);
  await clearPageDrafts(); mutateAsync.mockResolvedValue({ status: "started", task_id: "task-a" });
  taskState.data = { ...taskState.data, status: "problems_ready", pending_submission_file_name: null, submission_file_name: null, last_failed_job_id: null, student_count: 0 };
  const { container, router } = renderPage("task-a");
  fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["answer"], "local-answer.txt")] } });
  await saveDraft();
  await act(async () => { await router.navigate("/tasks/task-b/submissions/upload"); });
  expect(screen.queryByText("local-answer.txt")).not.toBeInTheDocument();
  await act(async () => { await router.navigate(-1); });
  await waitFor(() => expect(screen.getByText("local-answer.txt")).toBeInTheDocument());
  await act(async () => { await router.navigate("/settings/byok"); await router.navigate(-1); });
  await waitFor(() => expect(screen.getByText("local-answer.txt")).toBeInTheDocument());
  mutateAsync.mockClear();
  await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
  expect(await screen.findByText("progress page")).toBeInTheDocument();
  expect(mutateAsync).toHaveBeenCalledTimes(1);
  await act(async () => { await router.navigate(-1); });
  expect(screen.queryByText("local-answer.txt")).not.toBeInTheDocument();

});

describe("submitted input recovery", () => {
  beforeEach(async () => { inputState.input = null; await clearPageDrafts(); mutateAsync.mockReset(); retryMutateAsync.mockReset(); mutateAsync.mockResolvedValue({ status: "started" }); taskState.data = { ...taskState.data, status: "problems_ready", pending_submission_file_name: null, submission_file_name: null, last_failed_job_id: null, student_count: 0 }; });
  it("restores submitted archive and roster, and applies edited identity options", async () => {
    inputState.input = { job_id: "prior", stored_file_id: "saved-zip", filename: "saved.zip", available: true, identity_mode: "roster", roster_name: "saved-roster.csv", roster_count: 2, recognition_provider_id: "provider-default" };
    const { router } = renderPage();
    await screen.findByText("saved.zip");
    expect(screen.getByText("saved-roster.csv (2)")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" })).toHaveAttribute("aria-checked", "true");
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityManual" }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await screen.findByText("progress page");
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ file: null, storedFileId: "saved-zip", identityMode: "manual_review", reuseRosterFromJobId: null }));
    expect(retryMutateAsync).not.toHaveBeenCalled();
    await act(async () => { await router.navigate(-1); });
    await screen.findByText("saved.zip");
    expect(mutateAsync).toHaveBeenCalledTimes(1);
  });

});

async function saveDraft() { await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await waitFor(() => expect(screen.getByText(/已暂存 ·/)).toBeInTheDocument()); }
