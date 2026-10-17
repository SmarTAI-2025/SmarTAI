import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState, type ReactNode } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import { preflightProblemSource, startQuestionPreparation } from "@/api/problemSources";
import { createTask, getTask, getTaskResult, getTaskState, parseSubmissions, startGrading, updateProblem, updateStudentAnswer, updateStudentIdentity } from "@/api/tasks";
import { demoQuestions } from "@/test/fixtures/frontierQuestions";
import { I18nProvider as LocaleProvider, useI18n } from "@/i18n/I18nProvider";
import { beginFrontierSession, getFrontierToken, isFrontierTaskPath, rememberFrontierTask, setFrontierExitPending } from "@/lib/frontierSession";
import * as client from "@/api/client";
import { FrontierExitLink } from "@/components/auth/FrontierExitLink";
import { FrontierContext } from "@/lib/frontierContext";
import type { Correction, GradingSetupResponse, Task, TaskResultResponse, TaskStateSnapshot } from "@/types";
import { alignDemoProblems, FrontierLiveDemoPage } from "./FrontierLiveDemoPage";

function I18nProvider({ children }: { children: ReactNode }) {
  const [client] = useState(() => new QueryClient());
  return <QueryClientProvider client={client}><LocaleProvider>{children}</LocaleProvider></QueryClientProvider>;
}

vi.mock("@/api/tasks", () => ({
  createTask: vi.fn(),
  getTask: vi.fn(),
  getTaskResult: vi.fn(),
  getTaskState: vi.fn(),
  parseSubmissions: vi.fn(),
  startGrading: vi.fn(),
  updateProblem: vi.fn(),
  updateStudentAnswer: vi.fn(),
  updateStudentIdentity: vi.fn(),
}));

vi.mock("@/api/problemSources", () => ({
  preflightProblemSource: vi.fn(),
  startQuestionPreparation: vi.fn(),
}));

vi.mock("@/api/gradingSetup", () => ({
  getGradingSetup: vi.fn(),
  saveGradingSetup: vi.fn(),
}));

vi.mock("@/components/tasks/PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ title }: { title: string }) => <div role="document" aria-label={title} />,
}));

describe("FrontierLiveDemoPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    sessionStorage.clear();
    setFrontierExitPending(false);
    window.localStorage.setItem("smartai_locale", "en-US");
    beginFrontierSession("guest-access", "guest-owner");
    rememberFrontierTask("asg_demo123");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([false, true])("ignores late creation during confirmed exit (cleanup complete: %s)", async (cleanupComplete) => {
    const creation = deferred<TaskStateSnapshot>();
    const ending = deferred<{ status: string }>();
    vi.mocked(createTask).mockReturnValue(creation.promise);
    vi.spyOn(client, "postJSON").mockReturnValue(ending.promise);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderLive();
    await user.click(screen.getByRole("button", { name: /start real OCR \+ grading/i }));
    await user.click(screen.getByRole("link", { name: "Exit to login" }));
    if (cleanupComplete) {
      await act(async () => ending.resolve({ status: "ended" }));
      expect(screen.getByText("Public login")).toBeInTheDocument();
    }
    await act(async () => creation.resolve({ ...taskState("draft"), task_id: "asg_late" }));
    expect(isFrontierTaskPath("/tasks/asg_late")).toBe(false);
    expect(getGradingSetup).not.toHaveBeenCalled();
    if (!cleanupComplete) await act(async () => ending.resolve({ status: "ended" }));
    expect(screen.getByText("Public login")).toBeInTheDocument();
    expect(getFrontierToken()).toBeNull();
  });

  it("ignores a successful creation from a replaced guest session", async () => {
    const creation = deferred<TaskStateSnapshot>();
    vi.mocked(createTask).mockReturnValue(creation.promise);
    const user = userEvent.setup();
    renderLive();
    await user.click(screen.getByRole("button", { name: /start real OCR \+ grading/i }));
    act(() => beginFrontierSession("replacement-access", "replacement-owner"));
    await act(async () => creation.resolve({ ...taskState("draft"), task_id: "asg_oldowner" }));
    expect(isFrontierTaskPath("/tasks/asg_oldowner")).toBe(false);
    expect(getGradingSetup).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /start real OCR \+ grading/i })).toBeEnabled();
  });

  it("unlocks a restored run after changing language while recognition is pending", async () => {
    const recognition = deferred<TaskStateSnapshot>();
    vi.mocked(getTaskState)
      .mockResolvedValueOnce({ ...taskState("extracting_problems"), problem_count: 0 })
      .mockReturnValueOnce(recognition.promise)
      .mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await waitFor(() => expect(getTaskState).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Change language" }));
    expect(getTaskState).toHaveBeenCalledTimes(2);
    await act(async () => recognition.resolve(taskState("problems_ready")));
    expect(screen.getByRole("button", { name: "继续识别作答（保留待确认）" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重新开始" })).toBeEnabled();
    expect(getTask).toHaveBeenCalledTimes(1);
  });

  it.each(["zh-CN", "en-US"])("shows safe localized progress for recognition, grading, and unknown events (%s)", async (locale) => {
    window.localStorage.setItem("smartai_locale", locale);
    const zh = locale === "zh-CN";
    for (const [status, message, expected, activity] of [
      ["extracting_problems", "activity_responding", zh ? "请求已发出，等待模型响应" : "Request sent; awaiting model response", undefined],
      ["extracting_problems", "private provider diagnostic", zh ? "等待进度更新" : "Waiting for a progress update", undefined],
      ["grading", "private provider diagnostic", zh ? "请求已发出，等待模型响应" : "Request sent; awaiting model response", { sequence: 1, ts: 1, code: "responding" }],
    ] as const) {
      vi.mocked(getTaskState).mockResolvedValueOnce({ ...taskState(status), problem_count: 0, progress: {
        phase: status === "grading" ? "grading" : "extracting", current_step: "normalizing_ocr_output",
        total_students: 0, total_questions: 0, completed_units: 0, active: [],
        messages: [{ ts: 1, level: "info", message }], activity_latest: activity,
      } }).mockReturnValue(new Promise(() => {}));
      const view = renderLive("asg_demo123");
      const panel = (await screen.findByText(zh ? "后端进度" : "Backend progress")).parentElement!.parentElement!;
      expect(within(panel).getByText(expected)).toBeVisible();
      expect(within(panel).getByText(status === "grading" ? (zh ? "批改中" : "Grading") : (zh ? "题目识别" : "Processing questions"))).toBeVisible();
      expect(panel).not.toHaveTextContent(/normalizing_ocr_output|activity_responding|private provider diagnostic/);
      view.unmount();
    }
  });

  it.each(["zh-CN", "en-US"])("localizes recognized answer flags (%s)", async (locale) => {
    window.localStorage.setItem("smartai_locale", locale);
    const students = recognizedStudents();
    students["demo-1"].stu_ans[0].flag = ["recognition_needs_review", "recognition_low_confidence"];
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(), student_data: students });
    renderLive("asg_demo123");
    const expected = locale === "zh-CN"
      ? "识别结果需对照原件核对 · 本题识别置信度较低，请对照原件核对并确认"
      : "Check transcription against the original · Low recognition confidence; check the original and confirm";
    expect(await screen.findByText(expected)).toBeVisible();
    expect(screen.queryByText(/recognition_needs_review|recognition_low_confidence/)).not.toBeInTheDocument();
  });

  it.each(["zh-CN", "en-US"])("keeps the submission review and precise recovery link after a low-confidence grading block (%s)", async (locale) => {
    window.localStorage.setItem("smartai_locale", locale);
    const zh = locale === "zh-CN";
    const initial = { ...taskWithQuestions(), student_data: recognizedStudents() };
    const current = { ...taskWithQuestions(), student_data: recognizedStudents() };
    current.student_data["demo-1"].stu_ans[0].flag = ["recognition_needs_review", "recognition_low_confidence"];
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValueOnce(initial).mockResolvedValue(current);
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: zh ? "开始批改" : "Start grading" }))[0]);
    const alert = await screen.findByRole("alert");
    expect(screen.queryByRole("heading", { name: zh ? "检查本次识别的学生作答" : "Inspect this run's recognized submissions" })).toBeVisible();
    expect(screen.getByRole("link", { name: zh ? "在作答校对页查看" : "Open submission review" })).toHaveAttribute("href", "/tasks/asg_demo123/submissions");
    expect(within(alert).getByRole("link", { name: /Alex Chen/ })).toHaveAttribute("href", `/tasks/asg_demo123/students/demo-1?question=${current.student_data["demo-1"].stu_ans[0].q_id}`);
    expect(screen.queryByRole("link", { name: zh ? "返回修改配置" : "Back to edit settings" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: zh ? "开始批改" : "Start grading" })).toBeDisabled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
  });

  it.each([false, true])("keeps the replacement task locked when an old request settles (reject: %s)", async (reject) => {
    const oldRecognition = deferred<TaskStateSnapshot>();
    const nextRecognition = deferred<TaskStateSnapshot>();
    rememberFrontierTask("asg_next");
    vi.mocked(getTaskState).mockImplementation(async (id) => {
      if (id === "asg_next") return nextRecognition.promise;
      return oldRecognition.promise;
    });
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(), task_id: "asg_next" });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await waitFor(() => expect(getTaskState).toHaveBeenCalledWith("asg_demo123"));
    await user.click(screen.getByRole("link", { name: "Switch task" }));
    await waitFor(() => expect(getTaskState).toHaveBeenCalledWith("asg_next"));
    await act(async () => {
      if (reject) oldRecognition.reject(new Error("late task failure"));
      else oldRecognition.resolve(taskState("problems_ready"));
    });
    expect(screen.getByRole("button", { name: "Running…" })).toBeDisabled();
    expect(screen.queryByText("late task failure")).not.toBeInTheDocument();
    expect(getTask).not.toHaveBeenCalled();
    await act(async () => nextRecognition.resolve({ ...taskState("problems_ready"), task_id: "asg_next" }));
    expect(screen.getByRole("button", { name: /continue to submissions/i })).toBeEnabled();
    expect(screen.getByRole("link", { name: /^open task/i })).toHaveAttribute("href", "/tasks/asg_next");
  });

  it("stops polling after leaving for task details and restores on return", async () => {
    const recognition = deferred<TaskStateSnapshot>();
    vi.mocked(getTaskState)
      .mockResolvedValueOnce({ ...taskState("extracting_problems"), problem_count: 0 })
      .mockReturnValueOnce(recognition.promise)
      .mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await waitFor(() => expect(getTaskState).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("link", { name: /^open task/i }));
    await act(async () => recognition.resolve(taskState("problems_ready")));
    expect(screen.getByText("Task details")).toBeInTheDocument();
    expect(getTask).not.toHaveBeenCalled();
    await user.click(screen.getByRole("link", { name: "Return to Demo" }));
    expect(await screen.findByRole("button", { name: /continue to submissions/i })).toBeEnabled();
    expect(getTask).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("grades inline using the default or saved setup (configured: %s)", async (configured) => {
    const task = { ...taskWithQuestions(), student_data: recognizedStudents() };
    const setup = { ...gradingSetup(), configured, grading_setup: configured ? { ...gradingSetup().suggested_setup!, teacher_notes: "Keep my saved settings" } : null };
    const grading = deferred<TaskStateSnapshot>();
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(configured ? "grading_setup" : "submissions_ready"))
      .mockReturnValueOnce(grading.promise).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getGradingSetup).mockResolvedValue(setup);
    vi.mocked(saveGradingSetup).mockResolvedValue({ ...setup, configured: true, grading_setup: setup.suggested_setup, workflow_revision: 3 });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "grading-job" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(task));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    const buttons = await screen.findAllByRole("button", { name: "Start grading" });
    await user.dblClick(buttons[configured ? 1 : 0]);
    await waitFor(() => expect(startGrading).toHaveBeenCalledExactlyOnceWith("asg_demo123", {
      expectedWorkflowRevision: configured ? setup.workflow_revision : 3,
      requestId: expect.any(String),
    }));
    if (configured) expect(saveGradingSetup).not.toHaveBeenCalled();
    else expect(saveGradingSetup).toHaveBeenCalledExactlyOnceWith({
      taskId: "asg_demo123", expectedWorkflowRevision: setup.workflow_revision, gradingSetup: setup.suggested_setup,
    });
    expect(screen.getByRole("heading", { name: "Live grading run" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Running…" })).toBeDisabled();
    await act(async () => grading.resolve(taskState("graded")));
    expect(await screen.findByRole("heading", { name: "See class performance from live scores" })).toBeVisible();
    expect(updateProblem).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
    expect(updateStudentIdentity).not.toHaveBeenCalled();
  });

  it("retries a failed grading run in the same task without recognition or confirmation writes", async () => {
    const failed = { ...taskState("error"), grading_job_id: "grading-job", last_failed_job_id: "grading-job", error: "provider_upstream_unavailable" };
    vi.mocked(getTaskState).mockResolvedValueOnce(failed).mockResolvedValueOnce(failed).mockResolvedValue(taskState("graded"));
    vi.mocked(startGrading).mockResolvedValue({ status: "running", task_id: "asg_demo123", job_id: "retry-job" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult({ ...taskWithQuestions(), student_data: recognizedStudents() }));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click(await screen.findByRole("button", { name: "Retry failed items" }));
    await waitFor(() => expect(startGrading).toHaveBeenCalledExactlyOnceWith("asg_demo123", expect.objectContaining({ retryScope: "failed_only", expectedWorkflowRevision: failed.workflow_revision, requestId: expect.any(String) })));
    expect(await screen.findByRole("heading", { name: "See class performance from live scores" })).toBeVisible();
    expect(createTask).not.toHaveBeenCalled();
    expect(startQuestionPreparation).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(updateProblem).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
  });

  it("keeps a failed setup save on the Demo and starts only after an explicit successful retry", async () => {
    const task = { ...taskWithQuestions(), student_data: recognizedStudents() };
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState("submissions_ready")).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(saveGradingSetup).mockRejectedValueOnce(new client.APIError(409, "Conflict", { detail: { code: "workflow_revision_conflict" } }))
      .mockResolvedValue({ ...gradingSetup(), configured: true, grading_setup: gradingSetup().suggested_setup, workflow_revision: 4 });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "grading-job" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(task));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: "Start grading" }))[0]);
    const retry = await screen.findByRole("button", { name: "Retry grading" });
    expect(screen.getByRole("heading", { name: "Live grading run" })).toBeVisible();
    expect(startGrading).not.toHaveBeenCalled();
    await user.click(retry);
    expect(await screen.findByRole("heading", { name: "See class performance from live scores" })).toBeVisible();
    expect(startGrading).toHaveBeenCalledExactlyOnceWith("asg_demo123", { expectedWorkflowRevision: 4, requestId: expect.any(String) });
  });

  it("does not submit grading after leaving while the default setup is being saved", async () => {
    const saving = deferred<GradingSetupResponse>();
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(), student_data: recognizedStudents() });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(saveGradingSetup).mockReturnValue(saving.promise);
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: "Start grading" }))[0]);
    await waitFor(() => expect(saveGradingSetup).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("link", { name: /^open task/i }));
    await act(async () => saving.resolve({ ...gradingSetup(), configured: true, workflow_revision: 3 }));
    expect(screen.getByText("Task details")).toBeInTheDocument();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it("checks current blocking issues before saving defaults or grading", async () => {
    const task = { ...taskWithQuestions(), student_data: recognizedStudents() };
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValue({
      ...task, problem_data: { ...task.problem_data, q1: { ...task.problem_data.q1, stem: "" } },
    });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: "Start grading" }))[0]);
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Live grading run" })).toBeVisible();
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it("states the honest boundary between synthetic inputs and live processing", () => {
    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    expect(document.querySelector('[data-smartai-app-mark="silver"]')).toBeInTheDocument();
    expect(document.querySelector('[data-smartai-wordmark="white"]')).toBeInTheDocument();
    expect(screen.getByText("Synthetic inputs")).toBeInTheDocument();
    expect(screen.getByText("Real API")).toBeInTheDocument();
    expect(screen.getByText("Real OCR")).toBeInTheDocument();
    expect(screen.getByText("Real grading")).toBeInTheDocument();
    expect(screen.getByText(/Nothing on this page injects precomputed scores/i)).toBeInTheDocument();
    expect(screen.getByText(/no exposed model key/i)).toBeInTheDocument();
  });

  it("translates paused-run steps without restarting or presenting an error", async () => {
    vi.mocked(getTaskState).mockResolvedValue({ ...taskState("grading_paused"), error: null,
      grading_job_id: "stopped-run", last_failed_job_id: "stopped-run", problem_count: 4, student_count: 4 });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    expect(await screen.findByRole("link", { name: "Continue grading" })).toHaveAttribute("href", "/tasks/asg_demo123/grading/progress");
    await user.click(screen.getByRole("button", { name: "Change language" }));
    expect(screen.getByText("运行 AI 批改").closest("li")).not.toHaveClass("border-danger/30");
    expect(screen.getByText("创建专属 Demo 任务")).toBeInTheDocument();
    expect(screen.queryByText("Create a dedicated demo task")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "继续批改" })).toBeInTheDocument();
    expect(screen.getByText("批改暂停", { exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(getTaskState).toHaveBeenCalledTimes(1);
    expect(startGrading).not.toHaveBeenCalled();
    expect(createTask).not.toHaveBeenCalled();
  });

  it("offers an explicit real-run action", () => {
    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    expect(screen.getByRole("button", { name: /start real OCR \+ grading/i })).toBeEnabled();
  });

  it("explains when the daily demo allowance is exhausted", async () => {
    window.localStorage.setItem("smartai_locale", "zh-CN");
    vi.mocked(getTaskState)
      .mockResolvedValueOnce(taskState("parsing_submissions"))
      .mockResolvedValueOnce({
        ...taskState("error"),
        error: "shared_pool_daily_limit_reached",
      });
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}>
          <FrontierLiveDemoPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByText(/额度/)).toBeInTheDocument();
    expect(screen.queryByText(/submission_parse_failed/i)).not.toBeInTheDocument();
    expect(screen.getByText("识别混合作答").closest("li")).toHaveClass("border-danger/30");
    expect(screen.getByText("创建专属 Demo 任务").closest("li")).toHaveClass("border-accent/25");
  });

  it("keeps a question-preparation failure on its running step after polling stops", async () => {
    vi.mocked(getTaskState)
      .mockResolvedValueOnce({ ...taskState("extracting_problems"), problem_count: 0 })
      .mockResolvedValueOnce({ ...taskState("error"), error: "provider_unreachable" });

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}>
          <FrontierLiveDemoPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByText(/Cannot reach the model service/)).toBeInTheDocument();
    expect(screen.getByText("Prepare and review questions").closest("li")).toHaveClass("border-danger/30");
    expect(screen.getByText("Create a dedicated demo task").closest("li")).toHaveClass("border-accent/25");
    expect(screen.getByText("Recognize mixed submissions").closest("li")).not.toHaveClass("border-danger/30");
  });

  it.each([
    ["extract-job", "Prepare and review questions"],
    ["parse-job", "Recognize mixed submissions"],
    ["grading-job", "Run AI grading"],
  ])("restores the failed %s without moving the error to task creation", async (failedJobId, failedStep) => {
    vi.mocked(getTaskState).mockResolvedValue({
      ...taskState("error"),
      student_count: 4,
      extract_job_id: "extract-job",
      parse_job_id: "parse-job",
      grading_job_id: "grading-job",
      last_failed_job_id: failedJobId,
      error: "provider_unreachable",
    });

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}>
          <FrontierLiveDemoPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByText(/Cannot reach the model service/)).toBeInTheDocument();
    expect(screen.getByText(failedStep).closest("li")).toHaveClass("border-danger/30");
    expect(screen.getByText("Create a dedicated demo task").closest("li")).toHaveClass("border-accent/25");
    expect(getTask).not.toHaveBeenCalled();
    expect(startQuestionPreparation).not.toHaveBeenCalled();
  });

  it("preserves real recognized content instead of testing it against fixture keywords", () => {
    const problems = Object.values(taskWithQuestions().problem_data);
    problems[1] = { ...problems[1], stem: "Implement stable_softmax for values near 1000." };

    expect(alignDemoProblems(problems)[1].stem).toBe("Implement stable_softmax for values near 1000.");
  });

  it("aligns recognized questions by semantic identity instead of object order", () => {
    const problems = Object.values(taskWithQuestions().problem_data).reverse();

    expect(alignDemoProblems(problems).map((problem) => problem.q_id)).toEqual(["q1", "q2", "q3", "q4"]);
  });

  it("stops before rubric confirmation when recognized question numbers are duplicated", () => {
    const problems = Object.values(taskWithQuestions().problem_data);
    problems[1] = { ...problems[1], number: "Q1", q_id: "q1-copy" };

    expect(() => alignDemoProblems(problems)).toThrow(/unique Q1–Q4 numbering/i);
  });

  it("restores the same teacher-confirmation view without a resume button", async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}>
          <FrontierLiveDemoPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByRole("heading", { name: /review this run's generated materials/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume this task/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/teacher rubric confirmed/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /continue to submissions/i })).toHaveClass("h-12", "bg-primary");
    expect(screen.getByRole("button", { name: /start over/i })).toHaveClass("h-9", "bg-card");
  });

  it("does not infer teacher confirmation from a completed grading stage", async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());
    render(<I18nProvider><MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    expect(await screen.findByText(/see question review for confirmation status/i)).toBeInTheDocument();
    expect(screen.queryByText(/teacher rubric confirmed/i)).not.toBeInTheDocument();
  });

  it("keeps generated materials visible after returning under React StrictMode", async () => {
    const user = userEvent.setup();
    const completedTask = taskWithQuestions();
    Object.values(completedTask.problem_data).forEach((problem) => {
      problem.review_status = "confirmed";
    });
    vi.mocked(getTaskState).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(completedTask);
    vi.mocked(createTask).mockImplementation(() => new Promise(() => {}));

    render(
      <StrictMode>
        <I18nProvider>
          <MemoryRouter initialEntries={["/tasks/asg_demo123"]}>
            <Routes>
              <Route path="/tasks/:taskId" element={<Link to="/frontier/live?taskId=asg_demo123">Live Demo</Link>} />
              <Route path="/frontier/live" element={<FrontierLiveDemoPage />} />
            </Routes>
          </MemoryRouter>
        </I18nProvider>
      </StrictMode>,
    );

    await user.click(screen.getByRole("link", { name: "Live Demo" }));
    expect(await screen.findByRole("heading", { name: /review this run's generated materials/i })).toBeInTheDocument();
    expect(screen.getByText(/generated materials were teacher-confirmed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm generated materials/i })).not.toBeInTheDocument();
    expect(getTask).toHaveBeenCalledWith("asg_demo123");

    await user.click(screen.getByRole("button", { name: /start over/i }));
    expect(screen.queryByRole("heading", { name: /review this run's generated materials/i })).not.toBeInTheDocument();
  });

  it("runs the real API workflow in order without injecting fallback scores", async () => {
    const user = userEvent.setup();
    const recognizedTask = taskWithQuestions();
    recognizedTask.student_data = recognizedStudents();
    recognizedTask.problem_data.q2.stem = "Recognized Q2 wording from the live extraction.";
    vi.mocked(createTask).mockResolvedValue(taskState("draft"));
    vi.mocked(preflightProblemSource).mockResolvedValue({
      status: "ready",
      source_token: "source-questions",
      source: { kind: "upload", filename: "question_source.pdf", size_bytes: 7, sha256: "0".repeat(64) },
      structure_mode: "organized",
      requires_confirmation: false,
      candidate_summary: { matched: [], possible_matches: [], not_found: [], semantic_match_performed: false },
      workflow_revision: 0,
    });
    vi.mocked(startQuestionPreparation).mockResolvedValue({ status: "started", job_id: "job-questions" });
    vi.mocked(getTask).mockResolvedValue(recognizedTask);
    vi.mocked(updateProblem).mockResolvedValue({ status: "ok", q_id: "q", problem: taskWithQuestions().problem_data.q1, workflow_revision: 5 });
    vi.mocked(updateStudentAnswer).mockImplementation(async (_taskId, studentId, qId, patch) => ({ status: "ok", stu_id: studentId, q_id: qId, answer: recognizedTask.student_data[studentId].stu_ans.find((answer) => answer.q_id === qId)!, workflow_revision: (patch.expected_workflow_revision ?? 0) + 1 }));
    vi.mocked(parseSubmissions).mockResolvedValue({ status: "started", job_id: "job-submissions" });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(saveGradingSetup).mockResolvedValue({ ...gradingSetup(), configured: true, status: "saved" });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "job-grading" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(recognizedTask));
    vi.mocked(getTaskState).mockImplementation(async () => {
      if (vi.mocked(startGrading).mock.calls.length) return taskState("graded");
      if (vi.mocked(parseSubmissions).mock.calls.length) return taskState("submissions_ready");
      return taskState("problems_ready");
    });

    vi.stubGlobal("crypto", {
      randomUUID: () => "demo-run-id",
      subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).buffer) },
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("manifest.json")) {
        return {
          ok: true,
          json: async () => ({
            assets: [
              { path: "live/question_source.pdf", bytes: 7, sha256: "0".repeat(64) },
              { path: "live/submissions_raw.zip", bytes: 7, sha256: "0".repeat(64) },
            ],
          }),
        } as Response;
      }
      const fixture = new Blob(["fixture"]);
      Object.defineProperty(fixture, "arrayBuffer", {
        value: async () => new TextEncoder().encode("fixture").buffer,
      });
      return { ok: true, status: 200, blob: async () => fixture } as Response;
    }));

    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    await user.click(screen.getByRole("button", { name: /start real OCR \+ grading/i }));

    await waitFor(() => expect(startQuestionPreparation).toHaveBeenCalledTimes(1));
    const confirmTeacherMaterials = await screen.findByRole("button", { name: /continue to submissions/i });
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(screen.getByText(/come from this live preparation run/i)).toBeInTheDocument();
    await user.click(confirmTeacherMaterials);
    expect(updateProblem).not.toHaveBeenCalled();
    await waitFor(() => expect(parseSubmissions).toHaveBeenCalledTimes(1));
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(await screen.findByRole("heading", { name: /inspect this run's recognized submissions/i })).toBeInTheDocument();
    expect(screen.getByText("Alex Chen")).toBeVisible();
    expect(screen.getByText("Maya Lin")).toBeVisible();
    expect(screen.getByText("Jordan Rivera")).toBeVisible();
    expect(screen.getByText("Taylor Singh")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-1")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-2")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-3")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-4")).toBeVisible();
    const alexCard = screen.getByText("Alex Chen").closest("article");
    expect(alexCard).not.toBeNull();
    expect(within(alexCard!).getAllByText(/^Q[1-4]$/u).map((label) => label.textContent))
      .toEqual(["Q1", "Q2", "Q3", "Q4"]);
    await user.click(screen.getAllByRole("button", { name: "Start grading" })[0]);
    expect(await screen.findByRole("heading", { name: "See class performance from live scores" })).toBeVisible();
    expect(createTask).toHaveBeenCalledTimes(1);
    expect(preflightProblemSource).toHaveBeenCalledTimes(1);
    expect(startQuestionPreparation).toHaveBeenCalledTimes(1);
    expect(parseSubmissions).toHaveBeenCalledTimes(1);
    expect(updateProblem).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
    expect(saveGradingSetup).toHaveBeenCalledTimes(1);
    expect(startGrading).toHaveBeenCalledTimes(1);

  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function ChangeLocale() {
  const { setLocale } = useI18n();
  return <button onClick={() => setLocale("zh-CN")}>Change language</button>;
}

function renderLive(taskId?: string) {
  return render(<I18nProvider><FrontierContext.Provider value={true}>
    <MemoryRouter initialEntries={[taskId ? `/frontier/live?taskId=${taskId}` : "/frontier/live"]}>
      <Routes>
        <Route path="/frontier/live" element={<>
          <FrontierExitLink to="/login">Exit to login</FrontierExitLink>
          <Link to="/frontier/live?taskId=asg_next">Switch task</Link>
          <ChangeLocale /><FrontierLiveDemoPage />
        </>} />
        <Route path="/login" element={<p>Public login</p>} />
        <Route path="/tasks/:taskId" element={<>
          <p>Task details</p><Link to="/frontier/live?taskId=asg_demo123">Return to Demo</Link>
        </>} />
      </Routes>
    </MemoryRouter>
  </FrontierContext.Provider></I18nProvider>);
}

function taskState(status: TaskStateSnapshot["status"]): TaskStateSnapshot {
  return {
    task_id: "asg_demo123",
    name: "SmarTAI Live Demo",
    owner_id: "teacher-demo",
    status,
    workflow_revision: 1,
    problem_count: status === "draft" ? 0 : 4,
    student_count: ["submissions_ready", "grading", "graded"].includes(status) ? 4 : 0,
    kb_docs: {},
    kb_doc_count: 0,
    created_at: 1,
    updated_at: 1,
  };
}

function recognizedStudents(): Task["student_data"] {
  const names = ["Alex Chen", "Maya Lin", "Jordan Rivera", "Taylor Singh"];
  const filenames = [
    "DEMO-001_Alex-Chen_typeset.pdf",
    "DEMO-002_Maya-Lin_handwritten.png",
    "DEMO-003_Jordan-Rivera_mixed.pdf",
    "DEMO-004_Taylor-Singh_scan.png",
  ];
  return Object.fromEntries(Array.from({ length: 4 }, (_, index) => {
    const id = `demo-${index + 1}`;
    return [id, {
      stu_id: id,
      stu_name: names[index],
      source_filename: filenames[index],
      identity_status: "matched" as const,
      identity_match_method: "filename" as const,
      stu_ans: Object.values(taskWithQuestions().problem_data).reverse().map((problem) => ({
        q_id: problem.q_id,
        number: problem.number,
        type: problem.type,
        content: `Recognized answer for ${problem.q_id} by ${id}`,
        flag: [],
        review_status: "pending" as const,
      })),
    }];
  }));
}

function gradedResult(task: Task): TaskResultResponse {
  const correction = (qId: string, maxScore: number, index: number): Correction => ({
    q_id: qId,
    type: qId === "q4" ? "programming" : "calculation",
    score: Math.max(0, maxScore - index),
    provisional_score: Math.max(0, maxScore - index),
    max_score: maxScore,
    confidence: 0.82,
    comment: "Live model feedback",
    steps: [],
    expert_results: [],
    requires_human_review: index === 2,
    review_reasons: index === 2 ? ["low_confidence"] : [],
  });
  return {
    status: "completed",
    task_id: task.task_id,
    problem_data: task.problem_data,
    student_data: task.student_data,
    results: Object.values(task.student_data).map((student, studentIndex) => ({
      student_id: student.stu_id,
      student_name: student.stu_name,
      student_answers: student.stu_ans,
      corrections: Object.values(task.problem_data).map((problem, qIndex) => correction(problem.q_id, problem.max_score, (studentIndex + qIndex) % 3)),
    })),
  };
}

function taskWithQuestions(): Task {
  const base = taskState("problems_ready");
  const problem = (qId: string, number: string, stem: string) => ({
    q_id: qId,
    number,
    type: qId === "q4" ? "programming" : "calculation",
    stem,
    criterion: `Generated rubric for ${qId}`,
    max_score: qId === "q1" ? 5 : qId === "q2" ? 8 : qId === "q3" ? 7 : 10,
    reference_answer: `Generated answer for ${qId}`,
    solution_code: qId === "q4" ? "def stable_softmax(xs):\n    return []" : null,
    test_cases: qId === "q4" ? [{
      input: "[[]]",
      expected_output: "[]",
      description: "Generated empty-input test",
      source: "llm_generated" as const,
      sandbox_feasible: true,
    }] : null,
    review_status: "needs_review" as const,
  });
  return {
    ...base,
    problem_data: {
      q1: problem("q1", "1", demoQuestions[0].prompt),
      q2: problem("q2", "2", demoQuestions[1].prompt),
      q3: problem("q3", "3", demoQuestions[2].prompt),
      q4: problem("q4", "4", demoQuestions[3].prompt),
    },
    student_data: {},
  };
}

function gradingSetup(): GradingSetupResponse {
  return {
    task_id: "asg_demo123",
    task_status: "submissions_ready",
    workflow_revision: 2,
    configured: false,
    grading_setup: null,
    suggested_setup: {
      schema_version: 1,
      selected_provider_ids: ["demo:vision"],
      primary_provider_id: "demo:vision",
      aggregation_method: "single",
      multi_sample_n: 1,
      knowledge_scope: "none",
      strictness: 50,
      allow_partial_credit: true,
      feedback_tone: "neutral",
      feedback_length: "medium",
      feedback_language: "en",
      suggest_corrections: true,
      low_confidence_threshold: 0.6,
      teacher_notes: "",
    },
    grading_setup_fingerprint: null,
    grading_setup_updated_at: null,
    available_experts: [{
      provider_id: "demo:vision",
      provider_type: "demo",
      model: "vision",
      display_name: "Demo Vision",
      enabled: true,
      scope: "owner",
      is_shared: false,
      editable: true,
      max_concurrent: 1,
      rpm: 10,
    }],
    knowledge: { scope_options: ["none"], task_doc_count: 0, task_docs: [] },
    readiness: { ready: true, blocking_issues: [], warnings: [] },
  };
}
