import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";
import { SubmissionRecognitionProgressPage } from "./SubmissionRecognitionProgressPage";
import type { JobProgress } from "@/types/progress";
import { RecognitionActivity } from "@/components/tasks/RecognitionActivity";

const state = vi.hoisted(() => ({ status: "extracting_problems", operation: "running", locale: "zh-CN", stale: false, progress: null as JobProgress | null }));
const task = () => ({ task_id: "T", status: state.status, active_job_id: "job", active_operation_status: state.operation, workflow_revision: 1 });
vi.mock("@/api/hooks", () => ({
  useTask: () => ({ data: task(), refetch: vi.fn() }),
  useStageProviders: () => ({ data: [] }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: task(), refetch: vi.fn() }),
  useRetrySubmissionRecognition: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));
vi.mock("@/hooks/useTaskProgress", () => ({ useTaskProgress: () => ({ data: task(), progress: state.progress, percent: 30, isError: state.stale, refetch: vi.fn() }) }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/components/tasks/SubmissionSourceOutcomePanel", () => ({ SubmissionSourceOutcomePanel: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: state.locale, t: (key: string) => key }) }));

function Page({ kind }: { kind: string }) {
  return <MemoryRouter initialEntries={["/tasks/T/progress"]}><Routes><Route path="/tasks/:taskId/progress" element={kind === "questions" ? <ProblemRecognitionProgressPage /> : <SubmissionRecognitionProgressPage />} /></Routes></MemoryRouter>;
}
beforeEach(() => {
  state.locale = "zh-CN"; state.stale = false; state.operation = "running";
  state.progress = {
    job_id: "job", phase: "parsing", total_students: 10, total_questions: 7, completed_units: 3,
    active: [], messages: [{ ts: 100, level: "info", message: "activity_reading" }],
    total_steps: 10, completed_steps: 3, current_step: "generating_solutions",
    stage_metrics: { files_total: 10, files_processed: 3, solution_total_questions: 7, solution_completed_questions: 3 },
    model_waits: [{ model: "test-model", reason: "provider_rate_limited", attempt: 2, max_attempts: 3, retry_at: Date.now() / 1000 + 30 }],
    recognition_requests: [{ state: "responding", since: 100 }, { state: "retrying", reason: "timeout", attempt: 2, max_attempts: 3, retry_at: Date.now() / 1000 + 20 }],
  };
});

it.each(["operation_cancelled", "provider_timeout"])("does not present the last wait as ongoing after %s", (code) => {
  state.progress!.error_detail = code;
  state.progress!.messages = [{ ts: 100, level: "info", message: "activity_responding" }];
  render(<RecognitionActivity progress={state.progress} locale="zh-CN" incomplete />);
  const button = screen.getByRole("button", { name: /最近动态/ });
  expect(button).toHaveTextContent(code === "operation_cancelled" ? "本次识别已停止" : "本次识别未完成");
  expect(button).not.toHaveTextContent("等待模型响应");
  fireEvent.click(button);
  expect(within(screen.getByRole("region")).getByText("请求已发出，等待模型响应")).toBeVisible();
});

describe.each(["questions", "submissions"])("%s recognition activity", (kind) => {
  it.each(["zh-CN", "en-US"])("keeps progress visible during waits; expands history and resets for a new run (%s)", (locale) => {
    state.locale = locale;
    state.status = kind === "questions" ? "extracting_problems" : "parsing_submissions";
    const page = render(<Page kind={kind} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "30");
    expect(screen.getByText(kind === "questions" ? (locale === "zh-CN" ? "已完成 3/7 道大题" : "3/7 major questions completed") : /submissionProgressProcessed 3 \/ 10/)).toBeVisible();
    expect(screen.getByText(new RegExp(locale === "zh-CN" ? "1 个请求等待模型响应" : "1 requests awaiting model response"))).toBeVisible();
    expect(screen.getByText(new RegExp(locale === "zh-CN" ? "响应超时.*2/3" : "Response timed out.*2/3"))).toBeVisible();
    const toggle = screen.getByRole("button", { name: new RegExp(locale === "zh-CN" ? "最近动态" : "Recent activity") });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    const history = screen.getByRole("region", { name: locale === "zh-CN" ? "识别动态历史" : "Recognition activity history" });
    expect(history).toHaveClass("h-56", "overflow-y-auto");
    expect(within(history).getAllByRole("listitem")).toHaveLength(1);
    state.progress = { ...state.progress!, messages: [...state.progress!.messages, { ts: 101, level: "info", message: "activity_response_received" }] };
    page.rerender(<Page kind={kind} />);
    expect(within(history).getAllByRole("listitem")).toHaveLength(2);
    expect(toggle).toHaveTextContent(locale === "zh-CN" ? "模型已返回" : "Model response received");
    fireEvent.click(toggle);
    expect(screen.queryByRole("region", { name: /activity history|识别动态历史/ })).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "30");
    fireEvent.click(toggle);
    state.progress = { ...state.progress!, job_id: "new-job", messages: [] };
    page.rerender(<Page kind={kind} />);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("does not expose unknown raw text and does not pretend stale retry data is live", () => {
    state.status = kind === "questions" ? "extracting_problems" : "parsing_submissions";
    state.progress!.messages.push({ ts: 110, level: "info", message: "PRIVATE_PROMPT sk-secret internal-agent" });
    state.stale = true;
    render(<Page kind={kind} />);
    fireEvent.click(screen.getByRole("button", { name: /最近动态/ }));
    expect(document.body).not.toHaveTextContent("PRIVATE_PROMPT");
    expect(screen.getByText("暂时无法更新状态，保留上次进度")).toBeVisible();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "30");
    expect(screen.queryByText(/秒后重试/)).not.toBeInTheDocument();
  });

  it("retains completed progress when requeued and clears old retry hints after resuming", () => {
    state.status = kind === "questions" ? "extracting_problems" : "parsing_submissions";
    state.operation = "pending";
    const page = render(<Page kind={kind} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "30");
    expect(screen.queryByText(/秒后重试/)).not.toBeInTheDocument();
    state.operation = "running";
    state.progress!.recognition_requests = [{ state: "responding" }];
    page.rerender(<Page kind={kind} />);
    expect(screen.getByText(/1 个请求等待模型响应/)).toBeVisible();
    expect(screen.queryByText(/秒后重试/)).not.toBeInTheDocument();
  });

  it("shows aggregate request counts without repeating identical retry rows", () => {
    state.status = kind === "questions" ? "extracting_problems" : "parsing_submissions";
    state.progress!.recognition_requests = [{ state: "responding", count: 7 },
      { state: "retrying", count: 2, reason: "timeout", attempt: 2, max_attempts: 3 },
      { state: "retrying", count: 3, reason: "timeout", attempt: 2, max_attempts: 3 }];
    render(<Page kind={kind} />);
    expect(screen.getByText(/7 个请求等待模型响应/)).toBeVisible();
    expect(screen.getAllByText(/5 个请求等待重试/)).toHaveLength(1);
  });
});
