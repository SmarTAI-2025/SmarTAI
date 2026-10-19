import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, Outlet } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageDraftSession } from "@/hooks/usePageDraft";
import { clearPageDrafts, preparePageDraft, readPageDraft, writePageDrafts } from "@/lib/pageDraftStore";
import { initialSubmissionDraft, submissionDraftCodec } from "@/lib/taskPageDrafts";
import { APIError } from "@/api/client";
import { userFacingError } from "@/lib/taskActionGuards";
import { rememberImageReturn } from "@/lib/imageRecoveryNavigation";
import { AddSubmissionsPage } from "./AddSubmissionsPage";

const mutateAsync = vi.fn();
const retryMutateAsync = vi.fn();
const providers = vi.hoisted(() => ({ enabled: true }));
const copy = vi.hoisted(() => ({ locale: "zh-CN" }));
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
    error: null as string | null,
  },
}));

const inputState = vi.hoisted(() => ({ input: null as unknown }));
beforeEach(() => { vi.spyOn(window, "alert").mockImplementation(() => {}); });
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
  useI18n: () => ({ locale: copy.locale, t: (key: string) => key }),
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
  it.each(["zh-CN", "en-US"])("localizes upload network errors and keeps selected input (%s)", async locale => {
    copy.locale = locale;
    try {
      const { container } = renderPage();
      fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["answers"], "S003_Li.png", { type: "image/png" })] } });
      await waitFor(() => expect(screen.getByLabelText(locale === "zh-CN" ? "作答识别模型" : "Submission recognition model")).toHaveValue("provider-default"));
      mutateAsync.mockRejectedValueOnce(new APIError(0, "Network Error"));
      fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
      const alert = await screen.findByText(userFacingError(new APIError(0, "Network Error"), locale), { selector: "p[role='alert']" });
      expect(alert).not.toHaveTextContent("Network Error");
      expect(alert.textContent).toMatch(locale === "zh-CN" ? /网络|连接/ : /connect|network|reach/i);
      expect(screen.getByText("S003_Li.png")).toBeInTheDocument();
      expect(mutateAsync).toHaveBeenCalledOnce();
    } finally { copy.locale = "zh-CN"; }
  });
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
      error: null,
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
    expect(screen.getAllByRole("alert").some((item) => item.textContent?.includes("尚未选择作答文件"))).toBe(true);
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("尚未选择作答文件"));
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

  it.each([
    ["submissionUploadIdentityManual", "S1_Alex.pdf"],
    ["submissionUploadIdentityFilename", "answers.zip"],
  ])("clears only the stale roster requirement when switching to %s", async (mode, filename) => {
    const { container } = renderPage();
    const file = new File(["local selection fixture; no OCR"], filename);
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    const roster = screen.getByRole("radio", { name: "submissionUploadIdentityRoster" });
    const start = screen.getByRole("button", { name: "submissionUploadStart" });
    fireEvent.click(roster);
    fireEvent.click(start);
    expect(container.querySelector("#submission-roster-required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "submissionUploadRosterChoose" })).toHaveAttribute("aria-describedby", "submission-roster-required");
    expect(mutateAsync).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("radio", { name: mode }));
    expect(screen.getByRole("radio", { name: mode })).toHaveAttribute("aria-checked", "true");
    expect(container.querySelector("#submission-roster-required")).not.toBeInTheDocument();
    expect(screen.queryByText(/尚未选择学生名单/)).not.toBeInTheDocument();
    expect(screen.getByText(filename)).toBeInTheDocument();
    expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default");

    // Clearing the validation state must not merely hide it in other modes.
    fireEvent.click(roster);
    expect(container.querySelector("#submission-roster-required")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "submissionUploadRosterChoose" })).not.toHaveAttribute("aria-describedby");
    fireEvent.click(start);
    expect(container.querySelector("#submission-roster-required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "submissionUploadRosterChoose" })).toHaveFocus();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("retains the missing submission error and its accessible association across identity modes", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    for (const mode of ["submissionUploadIdentityManual", "submissionUploadIdentityFilename"]) {
      fireEvent.click(screen.getByRole("radio", { name: mode }));
      expect(container.querySelector("#submission-file-required")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "submissionUploadChoose" })).toHaveAttribute("aria-describedby", "submission-file-required");
      fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
      expect(mutateAsync).not.toHaveBeenCalled();
    }
  });

  it("clears stale roster errors when deleting the draft resets the identity mode", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["answer"], "S1_Alex.pdf")] } });
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(container.querySelector("#submission-roster-required")).toBeInTheDocument();
    const confirmation = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      fireEvent.click(screen.getByRole("button", { name: "删除本页草稿" }));
      await waitFor(() => expect(screen.getByRole("radio", { name: "submissionUploadIdentityFilename" })).toHaveAttribute("aria-checked", "true"));
      expect(container.querySelector("#submission-roster-required")).not.toBeInTheDocument();
      expect(screen.queryByText(/尚未选择学生名单/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
      expect(container.querySelector("#submission-roster-required")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
      expect(container.querySelector("#submission-file-required")).toBeInTheDocument();
      expect(mutateAsync).not.toHaveBeenCalled();
    } finally { confirmation.mockRestore(); }
  });

  it.each(["filename", "manual_review", "roster"] as const)("reconciles roster validation when restoring a %s draft", async (identityMode) => {
    const owner = "submission-teacher", scope = "submissions:task-1";
    const saved = { ...initialSubmissionDraft(), identityMode, recognitionProviderId: "provider-default",
      selectedFile: new File(["saved answer"], "saved.pdf"),
      rosterFile: identityMode === "roster" ? new File(["student_id,name\nS1,Alex"], "saved-roster.csv") : null };
    const prior = await readPageDraft(owner, scope, submissionDraftCodec);
    await writePageDrafts([preparePageDraft(owner, scope, "older-input-version", saved, submissionDraftCodec, prior.epoch, null)]);
    const { container } = renderPage();
    await screen.findByRole("button", { name: "核对后恢复旧草稿" });
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["current"], "current.pdf")] } });
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(container.querySelector("#submission-roster-required")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "核对后恢复旧草稿" }));
    await screen.findByText("saved.pdf");
    expect(container.querySelector("#submission-roster-required")).not.toBeInTheDocument();
    expect(screen.queryByText(/尚未选择学生名单/)).not.toBeInTheDocument();
    expect(container.querySelector('[aria-describedby="submission-roster-required"]')).not.toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledOnce());
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ identityMode, file: expect.objectContaining({ name: "saved.pdf" }),
      rosterFile: identityMode === "roster" ? expect.objectContaining({ name: "saved-roster.csv" }) : null }));
  });

  it("still requires an enabled model after identity mode changes", async () => {
    providers.enabled = false;
    const { container } = renderPage();
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["answer"], "S1_Alex.pdf")] } });
    for (const mode of ["submissionUploadIdentityRoster", "submissionUploadIdentityManual", "submissionUploadIdentityFilename"]) {
      fireEvent.click(screen.getByRole("radio", { name: mode }));
      fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
      expect(screen.getByRole("link", { name: "submissionUploadConfigureModels" })).toBeInTheDocument();
      expect(mutateAsync).not.toHaveBeenCalled();
    }
  });

  it("retains image capability recovery after identity mode changes", async () => {
    const { container } = renderPage();
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["scan"], "S1_Alex.pdf")] } });
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    mutateAsync.mockRejectedValue(new APIError(422, "image_recognition_unconfirmed", { detail: { code: "image_recognition_unconfirmed" } }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await screen.findByRole("button", { name: "暂存并去验证" });
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(container.querySelector("#submission-roster-required")).toBeInTheDocument();
    for (const mode of ["submissionUploadIdentityManual", "submissionUploadIdentityFilename"]) {
      fireEvent.click(screen.getByRole("radio", { name: mode }));
      expect(screen.getByRole("button", { name: "暂存并去验证" })).toBeInTheDocument();
      expect(screen.getByText("S1_Alex.pdf")).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("progress page")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "暂存并去验证" })).toBeInTheDocument();
  });

  it.each([
    ["submissionUploadIdentityRoster", "roster"],
    ["submissionUploadIdentityManual", "manual_review"],
    ["submissionUploadIdentityFilename", "filename"],
  ])("preserves selected files and submits once with only the roster appropriate to %s", async (label, identityMode) => {
    const { container } = renderPage();
    const file = new File(["answer selection fixture"], "answers.zip");
    const rosterFile = new File(["student_id,name\nS1,Alex"], "roster.csv");
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.change(container.querySelectorAll('input[type="file"]')[1]!, { target: { files: [rosterFile] } });
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityManual" }));
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    expect(screen.getByText("roster.csv")).toBeInTheDocument();
    expect(screen.getByText("answers.zip")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: label }));
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    let finish!: (result: unknown) => void;
    mutateAsync.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const start = screen.getByRole("button", { name: "submissionUploadStart" });
    fireEvent.click(start);
    fireEvent.click(start);
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      file, identityMode, rosterFile: identityMode === "roster" ? rosterFile : null,
      reuseRosterFromJobId: null, recognitionProviderId: "provider-default", expectedWorkflowRevision: 0,
    }));
    await act(async () => { finish({ status: "started" }); });
    expect(await screen.findByText("progress page")).toBeInTheDocument();
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
    expect(screen.getByText(/原文件已保留/)).toBeInTheDocument();
    expect(screen.queryByText("submissionUploadExisting")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.click(screen.getByRole("button", { name: "重试失败项" }));

    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledWith({
      taskId: "task-retry",
      jobId: "job-failed",
      recognitionProviderId: "provider-default",
      expectedWorkflowRevision: 7,
    }));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(await screen.findByText("progress page")).toBeInTheDocument();
  });

  it.each([
    [false, false], [true, false], [false, true], [true, true],
  ])("restarts a stopped run on the first click (restored input: %s, legacy: %s)", async (restored, legacy) => {
    taskState.data = { ...taskState.data, status: legacy ? "error" : "submissions_paused", error: legacy ? "operation_cancelled" : null,
      workflow_revision: 2, last_failed_job_id: "stopped-job", pending_submission_file_name: "scan.pdf" };
    if (restored) inputState.input = { job_id: "stopped-job", stored_file_id: "saved-file", filename: "scan.pdf",
      available: true, identity_mode: "filename", recognition_provider_id: "provider-default" };
    renderPage();
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("button", { name: restored ? "重新识别全部作答" : legacy ? "重试失败项" : "继续识别" }));
    await screen.findByText("progress page");
    expect(restored ? mutateAsync : retryMutateAsync).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      expectedWorkflowRevision: 2, acknowledgePossibleDuplicateCall: true,
    }));
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
  it.each(["error", "submissions_paused"])("requires a roster before reparsing a stored file from %s", async (status) => {
    taskState.data = { ...taskState.data, status, pending_submission_file_name: "saved.pdf", last_failed_job_id: "prior" };
    inputState.input = { job_id: "prior", stored_file_id: "saved-pdf", filename: "saved.pdf", available: true, identity_mode: "filename", recognition_provider_id: "provider-default" };
    const { container } = renderPage();
    await screen.findByText("saved.pdf");
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    fireEvent.click(screen.getByRole("button", { name: "重新识别全部作答" }));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(retryMutateAsync).not.toHaveBeenCalled();
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("尚未选择学生名单"));
    expect(container.querySelector("#submission-roster-required")).toHaveTextContent("尚未选择学生名单");
    expect(screen.getByRole("button", { name: "submissionUploadRosterChoose" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "submissionUploadRosterChoose" })).toHaveAttribute("aria-describedby", "submission-roster-required");
    expect(screen.getByText("saved.pdf")).toBeInTheDocument();

    const roster = new File(["student_id,name\nS1,Alex"], "roster.csv", { type: "text/csv" });
    fireEvent.change(container.querySelectorAll('input[type="file"]')[1]!, { target: { files: [roster] } });
    expect(screen.queryByText(/尚未选择学生名单/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新识别全部作答" }));
    await screen.findByText("progress page");
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ file: null, storedFileId: "saved-pdf", identityMode: "roster", rosterFile: roster, reuseRosterFromJobId: null }));
    expect(retryMutateAsync).not.toHaveBeenCalled();
  });

  it.each(["error", "submissions_paused"])("reuses the saved roster when reparsing a stored file from %s", async (status) => {
    taskState.data = { ...taskState.data, status, pending_submission_file_name: "saved.pdf", last_failed_job_id: "prior" };
    inputState.input = { job_id: "prior", stored_file_id: "saved-pdf", filename: "saved.pdf", available: true, identity_mode: "roster", roster_name: "saved-roster.csv", roster_count: 2, recognition_provider_id: "provider-default" };
    renderPage();
    await screen.findByText("saved-roster.csv (2)");
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    fireEvent.click(screen.getByRole("button", { name: "重新识别全部作答" }));
    await screen.findByText("progress page");
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ file: null, storedFileId: "saved-pdf", identityMode: "roster", rosterFile: null, reuseRosterFromJobId: "prior" }));
    expect(retryMutateAsync).not.toHaveBeenCalled();
  });

  it("requires reselection when the server says the failed job's original is unavailable", async () => {
    taskState.data = { ...taskState.data, status: "error", pending_submission_file_name: "saved.zip", last_failed_job_id: "prior" };
    inputState.input = { job_id: "prior", stored_file_id: null, filename: "saved.zip", available: false, identity_mode: "filename", roster_count: 0, recognition_provider_id: "provider-default" };
    const { container } = renderPage();
    await screen.findAllByText(/saved.zip.*请重新选择作答文件/);
    expect(screen.queryByText(/原文件已保留/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("请重新选择作答文件"));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(retryMutateAsync).not.toHaveBeenCalled();
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["answer"], "saved.zip")] } });
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    await screen.findByText("progress page");
    expect(mutateAsync).toHaveBeenCalledTimes(1);
  });
  it("keeps filename-only roster guidance relevant to the current mode and file", async () => {
    inputState.input = { job_id: "prior", stored_file_id: "saved-pdf", filename: "saved.pdf", available: true, identity_mode: "roster", roster_name: "missing-roster.csv", roster_count: 0, recognition_provider_id: "provider-default" };
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByLabelText("作答识别模型")).toHaveValue("provider-default"));
    expect(container.querySelector("#submission-roster-required")).toHaveTextContent("missing-roster.csv");
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("请重新选择名单文件"));
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityManual" }));
    expect(screen.queryByText(/请重新选择名单文件/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" }));
    expect(container.querySelector("#submission-roster-required")).toHaveTextContent("missing-roster.csv");
    fireEvent.change(container.querySelectorAll('input[type="file"]')[1]!, { target: { files: [new File(["student_id,name\nS1,Alex"], "roster.csv")] } });
    expect(screen.queryByText(/请重新选择名单文件/)).not.toBeInTheDocument();
    expect(screen.getByText("名单已就绪，无需重新选择。")).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
  });
  it("labels restored failed-job inputs as a full restart and applies edited identity options", async () => {
    taskState.data = { ...taskState.data, status: "error", pending_submission_file_name: "saved.zip", last_failed_job_id: "prior" };
    inputState.input = { job_id: "prior", stored_file_id: "saved-zip", filename: "saved.zip", available: true, identity_mode: "roster", roster_name: "saved-roster.csv", roster_count: 2, recognition_provider_id: "provider-default" };
    const { router } = renderPage();
    await screen.findByText("saved.zip");
    expect(screen.getByText("saved-roster.csv (2)")).toBeInTheDocument();
    expect(screen.getByText(/可直接重试，无需重新上传/)).toBeInTheDocument();
    expect(screen.getByText("名单已就绪，无需重新选择。")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "submissionUploadIdentityRoster" })).toHaveAttribute("aria-checked", "true");
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "submissionUploadIdentityManual" }));
    fireEvent.click(screen.getByRole("button", { name: "重新识别全部作答" }));
    await screen.findByText("progress page");
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ file: null, storedFileId: "saved-zip", identityMode: "manual_review", reuseRosterFromJobId: null }));
    expect(retryMutateAsync).not.toHaveBeenCalled();
    await act(async () => { await router.navigate(-1); });
    await screen.findByText("saved.zip");
    expect(mutateAsync).toHaveBeenCalledTimes(1);
  });

});

async function saveDraft() { await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await waitFor(() => expect(screen.getByText(/已暂存 ·/)).toBeInTheDocument()); }
