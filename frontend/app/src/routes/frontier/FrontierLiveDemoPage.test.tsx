import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState, type ReactNode } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import { initializeContentLanguage } from "@/api/contentLanguage";
import * as fixtures from "@/lib/frontierFixtures";
import { preflightProblemSource, startQuestionPreparation } from "@/api/problemSources";
import { createTask, getTask, getTaskResult, getTaskState, parseSubmissions, retryQuestionPreparation, retrySubmissionRecognition, startGrading, updateProblem, updateStudentAnswer, updateStudentIdentity } from "@/api/tasks";
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
  retryQuestionPreparation: vi.fn(),
  retrySubmissionRecognition: vi.fn(),
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
vi.mock("@/api/contentLanguage", async original => ({ ...await original<object>(), initializeContentLanguage: vi.fn() }));

vi.mock("@/components/tasks/PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ title }: { title: string }) => <div role="document" aria-label={title} />,
}));

describe("FrontierLiveDemoPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(initializeContentLanguage).mockImplementation(async (_taskId, language) => ({ language }));
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

  it.each(["zh-CN", "en-US"])("shows the immediate queue notice without progress and clears it when running (%s)", async locale => {
    vi.useFakeTimers();
    let view: ReturnType<typeof renderLive> | undefined;
    try {
      localStorage.setItem("smartai_locale", locale);
      let state = { ...taskState("extracting_problems"), problem_count: 0, progress: null, active_operation_status: "pending" };
      vi.mocked(getTaskState).mockImplementation(async () => state as TaskStateSnapshot);
      view = renderLive("asg_demo123");
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const copy = locale === "zh-CN" ? "当前正在排队，请耐心等待；也可保留此标签页，稍后查看。" : "You’re currently in the queue. Please wait, or keep this tab open and check back later.";
      expect(screen.getByText(copy)).toBeVisible();
      expect(screen.getByText(copy)).toHaveClass("mt-2", "text-xs", "leading-5", "text-muted-foreground");
      state = { ...state, active_operation_status: "running" };
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(screen.queryByText(copy)).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally { view?.unmount(); vi.useRealTimers(); }
  });

  it.each([
    ["extracting_problems", "pending"], ["extracting_problems", "running"],
    ["parsing_submissions", "pending"], ["parsing_submissions", "running"],
    ["grading", "pending"], ["grading", "running"],
  ] as const)("keeps observing %s/%s beyond eight minutes until completion", async (status, operationStatus) => {
    vi.useFakeTimers();
    let view: ReturnType<typeof renderLive> | undefined;
    try {
      const completed = status === "extracting_problems" ? "problems_ready" : status === "parsing_submissions" ? "submissions_ready" : "graded";
      let state = { ...taskState(status), problem_count: 0, active_operation_status: operationStatus } as TaskStateSnapshot;
      vi.mocked(getTaskState).mockImplementation(async () => state);
      vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(completed), student_data: recognizedStudents() });
      vi.mocked(getTaskResult).mockResolvedValue(gradedResult({ ...taskWithQuestions(), student_data: recognizedStudents() }));
      view = renderLive("asg_demo123");
      await act(async () => { await vi.advanceTimersByTimeAsync(9 * 60 * 1000); });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      const before = vi.mocked(getTaskState).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(vi.mocked(getTaskState).mock.calls.length).toBeGreaterThan(before);
      state = taskState(completed);
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      const finished = vi.mocked(getTaskState).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(getTaskState).toHaveBeenCalledTimes(finished);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      for (const method of [createTask, retryQuestionPreparation, retrySubmissionRecognition, parseSubmissions, startGrading]) expect(method).not.toHaveBeenCalled();
    } finally { view?.unmount(); vi.useRealTimers(); }
  });

  it.each(["pause", "error", "session expired", "unmount"])("stops long-running observation on %s", async terminal => {
    vi.useFakeTimers();
    let view: ReturnType<typeof renderLive> | undefined;
    try {
      vi.mocked(getTaskState).mockResolvedValue({ ...taskState("extracting_problems"), problem_count: 0 });
      view = renderLive("asg_demo123");
      await act(async () => { await vi.advanceTimersByTimeAsync(9 * 60 * 1000); });
      if (terminal === "unmount") view.unmount();
      else if (terminal === "session expired") vi.mocked(getTaskState).mockRejectedValue(new client.APIError(401, "Expired", { detail: { code: "frontier_demo_session_expired" } }));
      else vi.mocked(getTaskState).mockResolvedValue({ ...taskState(terminal === "pause" ? "problems_paused" : "error"), error: terminal === "error" ? "provider_timeout" : null });
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      const stopped = vi.mocked(getTaskState).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(getTaskState).toHaveBeenCalledTimes(stopped);
      if (terminal === "pause") expect(screen.getByRole("button", { name: "Continue recognition" })).toBeEnabled();
      if (terminal === "error" || terminal === "session expired") expect(screen.getByRole("alert")).toBeVisible();
      for (const method of [createTask, retryQuestionPreparation, retrySubmissionRecognition, parseSubmissions, startGrading]) expect(method).not.toHaveBeenCalled();
    } finally { view?.unmount(); vi.useRealTimers(); }
  });

  it.each([
    ["extracting_problems", "parsing_submissions"], ["extracting_problems", "submissions_ready"],
    ["extracting_problems", "grading"], ["extracting_problems", "graded"], ["extracting_problems", "finalized"],
    ["parsing_submissions", "grading"], ["parsing_submissions", "graded"], ["parsing_submissions", "finalized"],
    ["parsing_submissions", "problems_ready"], ["grading", "problems_ready"],
    ["parsing_submissions", "extracting_problems"], ["grading", "parsing_submissions"], ["grading", "submissions_ready"],
    ["extracting_problems", "draft"], ["parsing_submissions", "draft"], ["grading", "draft"],
  ] as const)("reconciles a watched %s stage changed elsewhere to %s without restarting work", async (initial, changed) => {
    vi.useFakeTimers();
    let view: ReturnType<typeof renderLive> | undefined;
    try {
      let state = { ...taskState(initial), problem_count: 0 };
      vi.mocked(getTaskState).mockImplementation(async () => state);
      vi.mocked(getTask).mockImplementation(async () => ({ ...taskWithQuestions(state.status), student_data: recognizedStudents() }));
      vi.mocked(getTaskResult).mockResolvedValue(gradedResult({ ...taskWithQuestions(), student_data: recognizedStudents() }));
      view = renderLive("asg_demo123");
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      state = taskState(changed);
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      if (changed === "extracting_problems" || changed === "parsing_submissions" || changed === "grading") {
        const observing = vi.mocked(getTaskState).mock.calls.length;
        await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
        expect(vi.mocked(getTaskState).mock.calls.length).toBeGreaterThan(observing);
        state = taskState(changed === "grading" ? "graded" : changed === "extracting_problems" ? "problems_ready" : "submissions_ready");
        await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      }
      const finished = vi.mocked(getTaskState).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(9 * 60 * 1000); });
      expect(getTaskState).toHaveBeenCalledTimes(finished);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      if (["graded", "finalized"].includes(state.status)) expect(getTaskResult).toHaveBeenCalledWith("asg_demo123");
      else if (state.status === "problems_ready") expect(screen.getByRole("button", { name: "Continue to submissions" })).toBeEnabled();
      else if (state.status === "submissions_ready") expect(screen.getAllByRole("button", { name: "Start grading" })[0]).toBeEnabled();
      else expect(screen.getByRole("button", { name: "Start real OCR + grading" })).toBeEnabled();
      for (const method of [createTask, preflightProblemSource, startQuestionPreparation, retryQuestionPreparation, retrySubmissionRecognition, parseSubmissions, startGrading]) expect(method).not.toHaveBeenCalled();
    } finally { view?.unmount(); vi.useRealTimers(); }
  });

  it.each(["en-US", "zh-CN"])("freezes %s on first click even when UI changes while task creation is pending", async initialLocale => {
    localStorage.setItem("smartai_locale", initialLocale);
    const creation = deferred<TaskStateSnapshot>();
    vi.mocked(createTask).mockReturnValue(creation.promise);
    vi.spyOn(fixtures, "fixtureFile").mockResolvedValue(new File(["synthetic"], "questions.pdf"));
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(preflightProblemSource).mockResolvedValue({ source_token: "source", workflow_revision: 0 } as any);
    vi.mocked(startQuestionPreparation).mockResolvedValue({ status: "started", job_id: "questions" });
    vi.mocked(getTaskState).mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());
    const user = userEvent.setup();
    renderLive();
    await user.click(screen.getByRole("button", { name: /Start real OCR \+ grading|开始真实 OCR.*批改/ }));
    await user.click(screen.getByRole("button", { name: "Change language" }));
    await act(async () => creation.resolve(taskState("draft")));
    await waitFor(() => expect(startQuestionPreparation).toHaveBeenCalledTimes(1));
    const language = initialLocale === "en-US" ? "en" : "zh";
    expect(initializeContentLanguage).toHaveBeenCalledExactlyOnceWith("asg_demo123", language);
    expect(startQuestionPreparation).toHaveBeenCalledWith(expect.objectContaining({ generationLanguage: language }));
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
    expect(screen.getByRole("button", { name: "继续识别作答" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重新开始" })).toBeEnabled();
    expect(getTask).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("only reloads prepared questions after a restore error until Continue is clicked (confirmed: %s)", async confirmed => {
    const task = taskWithQuestions();
    if (confirmed) Object.values(task.problem_data).forEach(problem => { problem.review_status = "confirmed"; });
    let parsed = false;
    vi.mocked(getTaskState).mockImplementation(async () => taskState(parsed ? "submissions_ready" : "problems_ready"));
    vi.mocked(getTask).mockRejectedValueOnce(new Error("Network unavailable while restoring questions")).mockResolvedValue(task);
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.spyOn(fixtures, "fixtureFile").mockResolvedValue(new File(["synthetic"], "submissions.zip"));
    vi.mocked(parseSubmissions).mockImplementation(async () => { parsed = true; return { status: "started", job_id: "parse-job" }; });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click(await screen.findByRole("button", { name: "Retry failed items" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start over" })).toBeEnabled());
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(getGradingSetup).not.toHaveBeenCalled();
    expect(fixtures.fixtureFile).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Continue to submissions" }));
    await waitFor(() => expect(parseSubmissions).toHaveBeenCalledTimes(1));
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
      const panel = (await screen.findByText(zh ? "处理进度" : "Progress")).parentElement!.parentElement!;
      expect(within(panel).getByText(expected)).toBeVisible();
      expect(within(panel).getByText(status === "grading" ? (zh ? "批改中" : "Grading") : (zh ? "题目识别" : "Processing questions"))).toBeVisible();
      expect(panel).not.toHaveTextContent(/normalizing_ocr_output|activity_responding|private provider diagnostic/);
      view.unmount();
    }
  });

  it.each(["zh-CN", "en-US"])("hides ordinary confidence flags in the simple Demo (%s)", async (locale) => {
    window.localStorage.setItem("smartai_locale", locale);
    const students = recognizedStudents();
    students["demo-1"].stu_ans[0].flag = ["recognition_needs_review", "recognition_low_confidence"];
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions("submissions_ready"), student_data: students });
    renderLive("asg_demo123");
    const expected = locale === "zh-CN"
      ? "识别结果需对照原件核对 · 本题识别置信度较低，请对照原件核对并确认"
      : "Check transcription against the original · Low recognition confidence; check the original and confirm";
    expect(await screen.findByRole("heading", { name: locale === "zh-CN" ? "本次识别的学生作答" : "Recognized submissions" })).toBeVisible();
    expect(screen.queryByText(expected)).not.toBeInTheDocument();
    expect(screen.queryByText(/recognition_needs_review|recognition_low_confidence/)).not.toBeInTheDocument();
  });

  it.each(["problems_ready", "submissions_ready", "grading_setup"] as const)("fills completed recognition despite intermediate counters (%s)", async (status) => {
    for (const locale of ["zh-CN", "en-US"]) {
      window.localStorage.setItem("smartai_locale", locale);
      for (const steps of [status === "problems_ready" ? { total_steps: 8, completed_steps: 7 } : { total_steps: 4, completed_steps: 1 }, {}]) {
        vi.mocked(getTaskState).mockResolvedValue({ ...taskState(status), progress: {
          phase: "done", total_students: 4, total_questions: 4, completed_units: 0,
          ...steps, active: [], messages: [{ ts: 1, level: "info", message: "activity_completed" }],
        } });
        vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(status), student_data: recognizedStudents() });
        const view = renderLive("asg_demo123");
        const bar = await screen.findByRole("progressbar");
        expect(bar).toHaveAttribute("aria-valuenow", "100");
        expect(bar.firstElementChild).toHaveStyle({ width: "100%" });
        expect(screen.getByRole("status")).toHaveTextContent(locale === "zh-CN" ? "本次识别已完成" : "Recognition completed");
        expect(startQuestionPreparation).not.toHaveBeenCalled();
        expect(parseSubmissions).not.toHaveBeenCalled();
        expect(startGrading).not.toHaveBeenCalled();
        view.unmount();
      }
    }
  });

  it.each(["extracting_problems", "parsing_submissions", "grading", "error"] as const)("does not promote unfinished work when the reporter finishes early (%s)", async (status) => {
    vi.mocked(getTaskState).mockResolvedValueOnce({ ...taskState(status), problem_count: 0, progress: {
      phase: "done", total_students: 4, total_questions: 4, completed_units: 0,
      total_steps: 4, completed_steps: 1, active: [], messages: [],
    } }).mockReturnValue(new Promise(() => {}));
    renderLive("asg_demo123");
    const bar = await screen.findByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(bar.firstElementChild).toHaveStyle({ width: "25%" });
  });

  it.each(["graded", "finalized", "problems_paused", "submissions_paused", "grading_paused"] as const)("uses the terminal status when activity has been cleared (%s)", async (status) => {
    const stopped = status.endsWith("_paused");
    for (const locale of ["zh-CN", "en-US"]) {
      window.localStorage.setItem("smartai_locale", locale);
      vi.mocked(getTaskState).mockResolvedValue({ ...taskState(status), problem_count: 0, progress: {
        phase: stopped ? "paused" : "done", total_students: 4, total_questions: 4, completed_units: stopped ? 2 : 16,
        total_steps: 4, completed_steps: 1,
        active: [], messages: [], activity_latest: null,
      } });
      vi.mocked(getTaskResult).mockResolvedValue(gradedResult({ ...taskWithQuestions(), student_data: recognizedStudents() }));
      const view = renderLive("asg_demo123");
      const panel = (await screen.findByText(locale === "zh-CN" ? "处理进度" : "Progress")).parentElement!.parentElement!;
      expect(within(panel).queryByText(stopped
        ? (locale === "zh-CN" ? "本次处理已停止" : "This run has stopped")
        : (locale === "zh-CN" ? "已完成" : "Completed"))).toBeVisible();
      expect(panel).not.toHaveTextContent(/等待进度更新|Waiting for a progress update/);
      expect(within(panel).getByRole("progressbar")).toHaveAttribute("aria-valuenow", stopped ? "25" : "100");
      expect(startGrading).not.toHaveBeenCalled();
      view.unmount();
    }
  });

  it.each(["zh-CN", "en-US"])("routes newly discovered answer blockers to submission review (%s)", async (locale) => {
    window.localStorage.setItem("smartai_locale", locale);
    const zh = locale === "zh-CN";
    const initial = { ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() };
    const current = { ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() };
    current.student_data["demo-1"].stu_ans[0].flag = ["recognition_failed"];
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValueOnce(initial).mockResolvedValue(current);
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: zh ? "开始批改" : "Start grading" }))[0]);
    const alert = await screen.findByRole("alert");
    expect(screen.queryByRole("heading", { name: zh ? "本次识别的学生作答" : "Recognized submissions" })).toBeVisible();
    expect(screen.getByRole("link", { name: zh ? "在作答校对页查看" : "Open submission review" })).toHaveAttribute("href", "/tasks/asg_demo123/submissions");
    expect(within(alert).getByRole("link", { name: zh ? "校对作答" : "Review submissions" })).toHaveAttribute("href", "/tasks/asg_demo123/submissions");
    expect(within(alert).queryByRole("button", { name: zh ? "重试失败项" : "Retry failed items" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: zh ? "返回修改配置" : "Back to edit settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: zh ? "开始批改" : "Start grading" })).not.toBeInTheDocument();
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
  });

  it.each(["empty stem", "missing rubric", "missing questions", "duplicate numbering", "identity", "confirmation"])("offers manual review immediately for %s without starting work", async kind => {
    const submissions = kind === "identity" || kind === "confirmation";
    const task = { ...taskWithQuestions(submissions ? "submissions_ready" : "problems_ready"), student_data: recognizedStudents() };
    if (kind === "empty stem") task.problem_data.q1.stem = "";
    if (kind === "missing rubric") task.problem_data.q1.criterion = "";
    if (kind === "missing questions") task.problem_data = {};
    if (kind === "duplicate numbering") task.problem_data.q2.number = "1";
    if (kind === "identity") task.student_data["demo-1"].stu_name = "[Unknown Student]";
    if (kind === "confirmation") task.student_data["demo-1"].stu_ans[0].flag = ["recognition_repaired_needs_confirmation"];
    vi.mocked(getTaskState).mockResolvedValue({ ...taskState(submissions ? "submissions_ready" : "problems_ready"), problem_count: Object.keys(task.problem_data).length });
    vi.mocked(getTask).mockResolvedValue(task);
    renderLive("asg_demo123");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("link", { name: submissions ? "Review submissions" : "Review questions" })).toHaveAttribute("href", `/tasks/asg_demo123/${submissions ? "submissions" : "questions"}`);
    expect(screen.queryByRole("button", { name: /Retry failed items|Continue to submissions|Start grading/ })).not.toBeInTheDocument();
    expect(preflightProblemSource).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it.each(["problems_ready", "submissions_ready"] as const)("reloads saved review on return to the same task, without treating an unsaved draft as a fix (%s)", async stage => {
    const submissions = stage === "submissions_ready";
    const task = { ...taskWithQuestions(stage), student_data: recognizedStudents() };
    if (submissions) task.student_data["demo-1"].stu_name = "";
    else task.problem_data.q1.stem = "";
    vi.mocked(getTaskState).mockResolvedValue(taskState(stage));
    vi.mocked(getTask).mockImplementation(async () => structuredClone(task));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    const reviewLabel = submissions ? "Review submissions" : "Review questions";
    await user.click(await screen.findByRole("link", { name: reviewLabel }));
    await user.click(screen.getByRole("link", { name: "Return to Demo" }));
    // No persisted correction: returning must still lead to manual review.
    await user.click(await screen.findByRole("link", { name: reviewLabel }));
    task.problem_data.q1.stem = "Saved question text";
    task.student_data["demo-1"].stu_name = "Saved student name";
    await user.click(screen.getByRole("link", { name: "Return to Demo" }));
    expect((await screen.findAllByRole("button", { name: submissions ? "Start grading" : "Continue to submissions" }))[0]).toBeEnabled();
    expect(getTask).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(createTask).not.toHaveBeenCalled();
    expect(preflightProblemSource).not.toHaveBeenCalled();
    expect(startQuestionPreparation).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it.each(["problems_ready", "submissions_ready"] as const)("keeps a newly failed recognition retryable when continuing stale %s review", async stage => {
    const task = { ...taskWithQuestions(), status: stage, student_data: recognizedStudents() };
    const submissions = stage === "submissions_ready";
    let retried = false;
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(stage)).mockImplementation(async () => retried
      ? { ...taskState(stage), workflow_revision: 3 }
      : { ...taskState("error"), workflow_revision: 2, error: "provider_timeout", last_failed_job_id: "failed-recognition",
        extract_job_id: submissions ? "successful-questions" : "failed-recognition", parse_job_id: submissions ? "failed-recognition" : null });
    vi.mocked(getTask).mockResolvedValueOnce(task).mockImplementation(async () => retried
      ? { ...task, workflow_revision: 3 }
      : { ...task, status: "error", workflow_revision: 2, error: "provider_timeout", problem_data: submissions ? task.problem_data : {}, student_data: {} });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    const retry = submissions ? retrySubmissionRecognition : retryQuestionPreparation;
    vi.mocked(retry).mockImplementation(async () => { retried = true; return { status: "started", job_id: "failed-recognition" }; });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: stage === "problems_ready" ? "Continue to submissions" : "Start grading" }))[0]);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("button", { name: "Retry failed items" })).toBeVisible();
    expect(within(alert).queryByRole("link", { name: /Review questions|Review submissions/ })).not.toBeInTheDocument();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
    await user.click(within(alert).getByRole("button", { name: "Retry failed items" }));
    expect((await screen.findAllByRole("button", { name: submissions ? "Start grading" : "Continue to submissions" }))[0]).toBeEnabled();
    expect(retry).toHaveBeenCalledExactlyOnceWith({ taskId: "asg_demo123", jobId: "failed-recognition", recognitionProviderId: "demo:vision", expectedWorkflowRevision: 2, acknowledgePossibleDuplicateCall: true });
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(createTask).not.toHaveBeenCalled();
  });

  it.each(["problems_ready", "submissions_ready"] as const)("reconciles recognition failure between state and content while restoring %s", async stage => {
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(stage)).mockResolvedValue({
      ...taskState("error"), workflow_revision: 2, error: "provider_timeout",
      last_failed_job_id: "failed-recognition", extract_job_id: "failed-recognition", parse_job_id: stage === "submissions_ready" ? "failed-recognition" : null,
    });
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(), status: "error", workflow_revision: 2,
      error: "provider_timeout", problem_data: {}, student_data: {} });
    renderLive("asg_demo123");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("button", { name: "Retry failed items" })).toBeEnabled();
    expect(within(alert).queryByRole("link")).not.toBeInTheDocument();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it.each(["problems_ready", "submissions_ready"] as const)("does not combine old content with a newer ready workflow revision (%s)", async stage => {
    const task = { ...taskWithQuestions(), status: stage, student_data: recognizedStudents() };
    const revised = structuredClone(task);
    revised.workflow_revision = 2;
    if (stage === "problems_ready") revised.problem_data.q1.stem = "";
    else revised.student_data["demo-1"].stu_name = "";
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(stage)).mockResolvedValue({ ...taskState(stage), workflow_revision: 2 });
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValueOnce(task).mockResolvedValue(revised);
    vi.spyOn(fixtures, "fixtureFile").mockResolvedValue(new File(["synthetic"], "submissions.zip"));
    vi.mocked(getGradingSetup).mockResolvedValue({ ...gradingSetup(), grading_setup: gradingSetup().suggested_setup });
    vi.mocked(parseSubmissions).mockRejectedValue(new Error("Unexpected recognition with stale reviewed content"));
    vi.mocked(startGrading).mockRejectedValue(new Error("Unexpected grading with stale reviewed content"));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: stage === "problems_ready" ? "Continue to submissions" : "Start grading" }))[0]);
    expect(await screen.findByRole("link", { name: stage === "problems_ready" ? "Review questions" : "Review submissions" })).toBeVisible();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
  });

  it("stops without a mutation when review revisions keep changing", async () => {
    const task = taskWithQuestions();
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState("problems_ready"))
      .mockResolvedValueOnce({ ...taskState("problems_ready"), workflow_revision: 2 })
      .mockResolvedValue({ ...taskState("problems_ready"), workflow_revision: 3 });
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValueOnce(task).mockResolvedValue({ ...task, workflow_revision: 2 });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click(await screen.findByRole("button", { name: "Continue to submissions" }));
    expect(within(await screen.findByRole("alert")).getByRole("button", { name: "Retry failed items" })).toBeEnabled();
    expect(getTask).toHaveBeenCalledTimes(3);
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it("observes grading started in another tab instead of submitting again", async () => {
    const task = { ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() };
    const grading = deferred<TaskStateSnapshot>();
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState("submissions_ready"))
      .mockResolvedValueOnce(taskState("grading")).mockReturnValueOnce(grading.promise).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValue({ ...task, status: "grading" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(task));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: "Start grading" }))[0]);
    expect(await screen.findByRole("button", { name: "Running…" })).toBeDisabled();
    await act(async () => grading.resolve(taskState("graded")));
    expect(await screen.findByRole("heading", { name: "Detailed grading results" })).toBeVisible();
    expect(startGrading).not.toHaveBeenCalled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
  });

  it.each(["problems_ready", "submissions_ready"] as const)("keeps a newly paused recognition resumable when continuing stale %s review", async stage => {
    const task = { ...taskWithQuestions(), status: stage, student_data: recognizedStudents() };
    const status = stage === "problems_ready" ? "problems_paused" : "submissions_paused";
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(stage)).mockResolvedValue(taskState(status));
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValue({ ...task, status, problem_data: {}, student_data: {} });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: stage === "problems_ready" ? "Continue to submissions" : "Start grading" }))[0]);
    expect(await screen.findByRole("button", { name: "Continue recognition" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it.each(["problems_ready", "submissions_ready"] as const)("observes newly running recognition without restarting stale %s review", async stage => {
    const task = { ...taskWithQuestions(), status: stage, student_data: recognizedStudents() };
    const status = stage === "problems_ready" ? "extracting_problems" : "parsing_submissions";
    const pending = deferred<TaskStateSnapshot>();
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(stage)).mockResolvedValueOnce(taskState(status)).mockReturnValueOnce(pending.promise);
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValue({ ...task, status, problem_data: {}, student_data: {} });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: stage === "problems_ready" ? "Continue to submissions" : "Start grading" }))[0]);
    expect(await screen.findByRole("button", { name: "Running…" })).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => pending.resolve(taskState(stage === "problems_ready" ? "problems_paused" : "submissions_paused")));
    expect(await screen.findByRole("button", { name: "Continue recognition" })).toBeEnabled();
    expect(preflightProblemSource).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
    expect(retryQuestionPreparation).not.toHaveBeenCalled();
    expect(retrySubmissionRecognition).not.toHaveBeenCalled();
  });

  it("clears stale submission review when another tab returns the task to question review", async () => {
    const task = { ...taskWithQuestions(), status: "submissions_ready" as const, student_data: recognizedStudents() };
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState("submissions_ready")).mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValue(taskWithQuestions());
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: "Start grading" }))[0]);
    expect(await screen.findByRole("button", { name: "Continue to submissions" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Start grading" })).not.toBeInTheDocument();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it("rechecks persisted questions before uploading submissions", async () => {
    const task = taskWithQuestions();
    vi.mocked(getTaskState).mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValueOnce(task).mockResolvedValue({
      ...task, problem_data: { ...task.problem_data, q1: { ...task.problem_data.q1, stem: "" } },
    });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click(await screen.findByRole("button", { name: "Continue to submissions" }));
    expect(await screen.findByRole("link", { name: "Review questions" })).toHaveAttribute("href", "/tasks/asg_demo123/questions");
    expect(parseSubmissions).not.toHaveBeenCalled();
  });

  it("uses shared review severity for missing supplementary materials", async () => {
    const task = taskWithQuestions();
    task.problem_data.q1.reference_answer = null;
    task.problem_data.q4.solution_code = null;
    task.problem_data.q4.test_cases = null;
    vi.mocked(getTaskState).mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValue(task);
    renderLive("asg_demo123");
    expect(await screen.findByRole("button", { name: "Continue to submissions" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
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

  it.each([false, true])("grades inline despite optional expert disagreement (configured: %s)", async (configured) => {
    const task = { ...taskWithQuestions(configured ? "grading_setup" : "submissions_ready"), student_data: recognizedStudents() };
    const setup = { ...gradingSetup(), configured, grading_setup: configured ? { ...gradingSetup().suggested_setup!, teacher_notes: "Keep my saved settings" } : null };
    const grading = deferred<TaskStateSnapshot>();
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState(configured ? "grading_setup" : "submissions_ready"))
      .mockResolvedValueOnce(taskState(configured ? "grading_setup" : "submissions_ready"))
      .mockReturnValueOnce(grading.promise).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getGradingSetup).mockResolvedValue(setup);
    vi.mocked(saveGradingSetup).mockResolvedValue({ ...setup, configured: true, grading_setup: setup.suggested_setup, workflow_revision: 3 });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "grading-job" });
    const result = gradedResult(task);
    const correction = result.results![0].corrections[0];
    correction.expert_results = [correction.max_score, correction.max_score - 4].map(score => ({
      provider: "expert", score, max_score: correction.max_score, confidence: .95, comment: "", steps: [],
    }));
    vi.mocked(getTaskResult).mockResolvedValue(result);
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
    expect(await screen.findByRole("heading", { name: "Detailed grading results" })).toBeVisible();
    expect(updateProblem).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
    expect(updateStudentIdentity).not.toHaveBeenCalled();
  });

  it.each(["error", "grading_paused"] as const)("retries %s grading in the same task without recognition or confirmation writes", async status => {
    const failed = { ...taskState(status), grading_job_id: "grading-job", last_failed_job_id: "grading-job", error: status === "error" ? "provider_upstream_unavailable" : null };
    vi.mocked(getTaskState).mockResolvedValueOnce(failed).mockResolvedValueOnce(failed).mockResolvedValue(taskState("graded"));
    vi.mocked(startGrading).mockResolvedValue({ status: "running", task_id: "asg_demo123", job_id: "retry-job" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult({ ...taskWithQuestions(), student_data: recognizedStudents() }));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click(await screen.findByRole("button", { name: status === "error" ? "Retry failed items" : "Continue grading" }));
    await waitFor(() => expect(startGrading).toHaveBeenCalledExactlyOnceWith("asg_demo123", { retryScope: "failed_only", expectedWorkflowRevision: failed.workflow_revision, requestId: expect.any(String) }));
    expect(await screen.findByRole("heading", { name: "Detailed grading results" })).toBeVisible();
    expect(createTask).not.toHaveBeenCalled();
    expect(startQuestionPreparation).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(updateProblem).not.toHaveBeenCalled();
    expect(updateStudentAnswer).not.toHaveBeenCalled();
  });

  it.each(["zh-CN", "en-US"])("shows only an inline retry in the error card and keeps bottom task controls (%s)", async locale => {
    localStorage.setItem("smartai_locale", locale);
    vi.mocked(getTaskState).mockResolvedValue({ ...taskState("error"), error: "provider_rate_limited", grading_job_id: "failed", last_failed_job_id: "failed" });
    renderLive("asg_demo123");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getAllByRole("button")).toHaveLength(1);
    expect(within(alert).getByRole("button", { name: locale === "zh-CN" ? "重试失败项" : "Retry failed items" })).toBeEnabled();
    expect(within(alert).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /返回修改配置|Back to edit settings|查看进度与恢复|View progress and recovery|人工继续处理|Continue manually/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: locale === "zh-CN" ? "打开任务" : "Open task" })).toBeVisible();
    expect(screen.getByRole("button", { name: locale === "zh-CN" ? "重新开始" : "Start over" })).toBeEnabled();
  });

  it.each([ ["questions", false], ["submissions", false], ["questions", true], ["submissions", true] ] as const)("retries %s using the existing job without reuploading or leaving the Demo (paused: %s)", async (stage, paused) => {
    const submissions = stage === "submissions";
    const retry = submissions ? retrySubmissionRecognition : retryQuestionPreparation;
    const failed = { ...taskState(paused ? submissions ? "submissions_paused" : "problems_paused" : "error"), error: paused ? null : "provider_upstream_unavailable", last_failed_job_id: "failed", extract_job_id: submissions ? "done" : "failed", parse_job_id: submissions ? "failed" : null };
    const request = deferred<{ status: string; job_id: string }>();
    vi.mocked(getTaskState).mockResolvedValueOnce(failed).mockResolvedValueOnce({ ...failed, workflow_revision: 7 })
      .mockResolvedValue(taskState(submissions ? "submissions_ready" : "problems_ready"));
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(submissions ? "submissions_ready" : "problems_ready"), student_data: submissions ? recognizedStudents() : {} });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(retry).mockReturnValue(request.promise);
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.dblClick(await screen.findByRole("button", { name: paused ? "Continue recognition" : "Retry failed items" }));
    await waitFor(() => expect(retry).toHaveBeenCalledExactlyOnceWith({ taskId: "asg_demo123", jobId: "failed", recognitionProviderId: "demo:vision", expectedWorkflowRevision: 7, acknowledgePossibleDuplicateCall: true }));
    await act(async () => request.resolve({ status: "started", job_id: "retry" }));
    expect((await screen.findAllByRole("button", { name: submissions ? "Start grading" : "Continue to submissions" }))[0]).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Live grading run" })).toBeVisible();
    expect(createTask).not.toHaveBeenCalled();
    expect(preflightProblemSource).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it.each(["questions", "submissions"] as const)("retries uncertain %s in the quick Demo without an extra confirmation", async stage => {
    const submissions = stage === "submissions";
    const retry = submissions ? retrySubmissionRecognition : retryQuestionPreparation;
    const failed = { ...taskState("error"), workflow_revision: 7, error: "provider_submit_uncertain",
      extract_job_id: submissions ? "done" : "failed", parse_job_id: submissions ? "failed" : null, last_failed_job_id: "failed" };
    vi.mocked(getTaskState).mockResolvedValueOnce(failed).mockResolvedValueOnce(failed)
      .mockResolvedValue(taskState(submissions ? "submissions_ready" : "problems_ready"));
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions(submissions ? "submissions_ready" : "problems_ready"), student_data: submissions ? recognizedStudents() : {} });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(retry).mockResolvedValue({ status: "started", job_id: "retry" });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    expect(await screen.findByRole("button", { name: "Retry failed items" })).toBeEnabled();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await user.dblClick(screen.getByRole("button", { name: "Retry failed items" }));
    await waitFor(() => expect(retry).toHaveBeenCalledExactlyOnceWith({ taskId: "asg_demo123", jobId: "failed", recognitionProviderId: "demo:vision", expectedWorkflowRevision: 7, acknowledgePossibleDuplicateCall: true }));
    expect((await screen.findAllByRole("button", { name: submissions ? "Start grading" : "Continue to submissions" }))[0]).toBeEnabled();
    expect(createTask).not.toHaveBeenCalled();
    expect(preflightProblemSource).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(startGrading).not.toHaveBeenCalled();
  });

  it.each(["questions", "submissions"] as const)("keeps a rejected %s retry recoverable on the quick Demo", async stage => {
    const submissions = stage === "submissions";
    const retry = submissions ? retrySubmissionRecognition : retryQuestionPreparation;
    vi.mocked(getTaskState).mockResolvedValue({ ...taskState("error"), error: "provider_submit_uncertain",
      extract_job_id: submissions ? "done" : "failed", parse_job_id: submissions ? "failed" : null, last_failed_job_id: "failed" });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(retry).mockRejectedValue(new client.APIError(409, "Conflict", { detail: { code: "provider_submit_uncertain" } }));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click(await screen.findByRole("button", { name: "Retry failed items" }));
    await waitFor(() => expect(retry).toHaveBeenCalledTimes(1));
    expect(retry).toHaveBeenCalledWith(expect.objectContaining({ acknowledgePossibleDuplicateCall: true }));
    expect(await screen.findByRole("button", { name: "Retry failed items" })).toBeEnabled();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Live grading run" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Start over" })).toBeEnabled();
  });

  it("keeps a failed setup save on the Demo and starts only after an explicit successful retry", async () => {
    const task = { ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() };
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState("submissions_ready"))
      .mockResolvedValueOnce(taskState("submissions_ready")).mockResolvedValueOnce(taskState("submissions_ready"))
      .mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(saveGradingSetup).mockRejectedValueOnce(new client.APIError(409, "Conflict", { detail: { code: "workflow_revision_conflict" } }))
      .mockResolvedValue({ ...gradingSetup(), configured: true, grading_setup: gradingSetup().suggested_setup, workflow_revision: 4 });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "grading-job" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(task));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    await user.click((await screen.findAllByRole("button", { name: "Start grading" }))[0]);
    const retry = await screen.findByRole("button", { name: "Retry failed items" });
    expect(screen.getByRole("heading", { name: "Live grading run" })).toBeVisible();
    expect(startGrading).not.toHaveBeenCalled();
    await user.click(retry);
    expect(await screen.findByRole("heading", { name: "Detailed grading results" })).toBeVisible();
    expect(startGrading).toHaveBeenCalledExactlyOnceWith("asg_demo123", { expectedWorkflowRevision: 4, requestId: expect.any(String) });
  });

  it("does not submit grading after leaving while the default setup is being saved", async () => {
    const saving = deferred<GradingSetupResponse>();
    vi.mocked(getTaskState).mockResolvedValue(taskState("submissions_ready"));
    vi.mocked(getTask).mockResolvedValue({ ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() });
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
    const task = { ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() };
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

  it.each(["zh-CN", "en-US"])("keeps content tabs independent from grading and switches only when a stage completes (%s)", async locale => {
    localStorage.setItem("smartai_locale", locale);
    const zh = locale === "zh-CN";
    const task = { ...taskWithQuestions("submissions_ready"), student_data: recognizedStudents() };
    const waiting = deferred<TaskStateSnapshot>();
    const setup = gradingSetup();
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getTaskState).mockResolvedValueOnce(taskState("submissions_ready"))
      .mockResolvedValueOnce(taskState("submissions_ready")).mockReturnValueOnce(waiting.promise).mockResolvedValue(taskState("graded"));
    vi.mocked(getGradingSetup).mockResolvedValue({ ...setup, configured: true, grading_setup: setup.suggested_setup });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "grade" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(task));
    const user = userEvent.setup();
    renderLive("asg_demo123");
    const answerTab = await screen.findByRole("tab", { name: zh ? "学生作答" : "Submissions" });
    expect(answerTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    await user.click(screen.getByRole("tab", { name: zh ? "题目资料" : "Materials" }));
    await user.click(screen.getAllByRole("button", { name: zh ? "开始批改" : "Start grading" })[0]);
    await waitFor(() => expect(getTaskState).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("tab", { name: zh ? "题目资料" : "Materials" })).toHaveAttribute("aria-selected", "true");
    await user.click(answerTab);
    expect(screen.getByText("Recognized answer for q1 by demo-1")).toBeVisible();
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    await user.click(screen.getByRole("tab", { name: zh ? "题目资料" : "Materials" }));
    expect(screen.getByRole("heading", { name: zh ? "本次生成的题目资料" : "Generated question materials" })).toBeVisible();
    expect(startGrading).toHaveBeenCalledTimes(1);
    await act(async () => waiting.resolve(taskState("graded")));
    const gradingTab = await screen.findByRole("tab", { name: zh ? "批改详情" : "Grading details" });
    expect(gradingTab).toHaveAttribute("aria-selected", "true");
    expect(within(screen.getByRole("tablist", { name: zh ? "演示内容" : "Demo content" })).getAllByRole("tab")).toHaveLength(4);
    await user.click(screen.getByRole("tab", { name: zh ? "学情分析" : "Learning analysis" }));
    expect(screen.getByRole("heading", { name: zh ? "从真实分数看到班级表现" : "See class performance from live scores" })).toBeVisible();
    const charts = screen.getByRole("tablist", { name: zh ? "选择分析图" : "Choose analysis chart" });
    expect(within(charts).getAllByRole("tab")).toHaveLength(4);
    await user.click(within(charts).getByRole("tab", { name: "04" }));
    await user.click(gradingTab);
    await user.click(screen.getByRole("tab", { name: zh ? "学情分析" : "Learning analysis" }));
    expect(within(charts).getByRole("tab", { name: "04" })).toHaveAttribute("aria-selected", "true");
    expect(getTaskResult).toHaveBeenCalledTimes(1);
    expect(startGrading).toHaveBeenCalledTimes(1);
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(saveGradingSetup).not.toHaveBeenCalled();
  });

  it("loads saved answers when another tab advances recognition directly to grading", async () => {
    const waiting = deferred<TaskStateSnapshot>();
    const task = { ...taskWithQuestions("grading"), student_data: recognizedStudents() };
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getTaskState).mockResolvedValueOnce({ ...taskState("parsing_submissions"), problem_count: 0 })
      .mockResolvedValueOnce(taskState("grading")).mockReturnValue(waiting.promise);
    const current = renderLive("asg_demo123");
    expect(await screen.findByRole("tab", { name: "Submissions" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Recognized answer for q1 by demo-1")).toBeVisible();
    expect(getTask).toHaveBeenCalledTimes(1);
    expect(startGrading).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    current.unmount();
    await act(async () => waiting.resolve(taskState("graded")));
  });

  it("restores both recognized-content tabs while an existing grading job is still running", async () => {
    const waiting = deferred<TaskStateSnapshot>();
    const task = { ...taskWithQuestions("grading"), student_data: recognizedStudents() };
    vi.mocked(getTask).mockResolvedValue(task);
    vi.mocked(getTaskState).mockResolvedValueOnce({ ...taskState("grading"), problem_count: 0 }).mockReturnValue(waiting.promise);
    const user = userEvent.setup();
    const current = renderLive("asg_demo123");
    expect(await screen.findByRole("tab", { name: "Submissions" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "Materials" }));
    expect(screen.getByRole("heading", { name: "Generated question materials" })).toBeVisible();
    expect(startGrading).not.toHaveBeenCalled();
    expect(parseSubmissions).not.toHaveBeenCalled();
    current.unmount();
    await act(async () => waiting.resolve(taskState("graded")));
    expect(getTaskResult).not.toHaveBeenCalled();
  });

  it("labels demo samples without implementation commentary", () => {
    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    expect(document.querySelector('[data-smartai-app-mark="silver"]')).toBeInTheDocument();
    expect(document.querySelector('[data-smartai-wordmark="white"]')).toBeInTheDocument();
    expect(screen.getByText("Demo samples")).toBeInTheDocument();
    expect(screen.getByText("Task workflow")).toBeInTheDocument();
    expect(screen.getByText("Text recognition")).toBeInTheDocument();
    expect(screen.getByText("AI grading")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing on this page injects precomputed scores/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no exposed model key/i)).not.toBeInTheDocument();
  });

  it("translates paused-run steps without restarting or presenting an error", async () => {
    vi.mocked(getTaskState).mockResolvedValue({ ...taskState("grading_paused"), error: null,
      grading_job_id: "stopped-run", last_failed_job_id: "stopped-run", problem_count: 4, student_count: 4 });
    const user = userEvent.setup();
    renderLive("asg_demo123");
    expect(await screen.findByRole("button", { name: "Continue grading" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Change language" }));
    expect(screen.getByText("运行 AI 批改").closest("li")).not.toHaveClass("border-danger/30");
    expect(screen.getByText("创建专属 Demo 任务")).toBeInTheDocument();
    expect(screen.queryByText("Create a dedicated demo task")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续批改" })).toBeInTheDocument();
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

    expect(await screen.findByRole("heading", { name: /generated question materials/i })).toBeInTheDocument();
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
    expect(await screen.findByRole("heading", { name: /generated question materials/i })).toBeInTheDocument();
    expect(screen.getByText(/generated materials were teacher-confirmed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm generated materials/i })).not.toBeInTheDocument();
    expect(getTask).toHaveBeenCalledWith("asg_demo123");

    await user.click(screen.getByRole("button", { name: /start over/i }));
    expect(screen.queryByRole("heading", { name: /generated question materials/i })).not.toBeInTheDocument();
  });

  it("runs the same API workflow through ordinary low confidence without injecting fallback scores", async () => {
    const user = userEvent.setup();
    const recognizedTask = taskWithQuestions();
    recognizedTask.student_data = recognizedStudents();
    recognizedTask.problem_data.q1.preparation_issues = [{ issue_id: "source-low", q_id: "q1", field: "stem", code: "low_confidence", severity: "warning", status: "open", details: { confidence: 0.5 } }];
    for (const student of Object.values(recognizedTask.student_data)) for (const answer of student.stu_ans) answer.flag = ["recognition_low_confidence"];

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
    vi.mocked(getTask).mockImplementation(async () => ({ ...recognizedTask,
      status: vi.mocked(startGrading).mock.calls.length ? "graded" : vi.mocked(parseSubmissions).mock.calls.length ? "submissions_ready" : "problems_ready",
    }));
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
    expect(initializeContentLanguage).toHaveBeenCalledWith("asg_demo123", "en");
    expect(startQuestionPreparation).toHaveBeenCalledWith(expect.objectContaining({ generationLanguage: "en" }));
    const confirmTeacherMaterials = await screen.findByRole("button", { name: /continue to submissions/i });
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(screen.getByText(/were generated live/i)).toBeInTheDocument();
    await user.click(confirmTeacherMaterials);
    expect(updateProblem).not.toHaveBeenCalled();
    await waitFor(() => expect(parseSubmissions).toHaveBeenCalledTimes(1));
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(await screen.findByRole("heading", { name: /recognized submissions/i })).toBeInTheDocument();
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
    expect(await screen.findByRole("heading", { name: "Detailed grading results" })).toBeVisible();
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
  const { locale, setLocale } = useI18n();
  return <button onClick={() => setLocale(locale === "en-US" ? "zh-CN" : "en-US")}>Change language</button>;
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
        <Route path="/tasks/:taskId/:review" element={<>
          <p>Existing review page</p><Link to="/frontier/live?taskId=asg_demo123">Return to Demo</Link>
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

function taskWithQuestions(status: TaskStateSnapshot["status"] = "problems_ready"): Task {
  const base = taskState(status);
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
