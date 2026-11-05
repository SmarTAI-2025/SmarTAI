import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";
import type { JobProgress } from "@/types";

const { retryPreparation, manualPreparation } = vi.hoisted(() => ({ retryPreparation: vi.fn(), manualPreparation: vi.fn() }));
let questionProviderId = "";
let failureCode = "provider_timeout";
let failedPages: number[] = [];
let generationProgress: Partial<JobProgress> = {};
beforeEach(() => { vi.clearAllMocks(); questionProviderId = ""; failureCode = "provider_timeout"; failedPages = []; generationProgress = {}; });

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({ data: [], isError: false, isLoading: false }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: retryPreparation }),
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: manualPreparation }),
  useTask: () => ({
    data: {
      task_id: "task-1",
      status: "error",
      workflow_revision: 3,
      last_failed_job_id: "job-1",
      error: failureCode,
    },
    error: null,
    isFetching: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({
    data: { workflow_revision: 3, question_recognition_provider_id: questionProviderId, status: "error", active_job_id: null, last_failed_job_id: "job-1", error: failureCode },
    error: null,
    isFetching: false,
    progress: {
      phase: "error",
      error_detail: failureCode,
      recognition_failure: { failed_pages: failedPages },
      question_labels: { q2: "1.1.7" },
      failed_question_ids: ["q2"],
      question_error_codes: { q2: "provider_timeout" },
      messages: [],
      ...generationProgress,
    },
    refetch: vi.fn(),
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

describe("ProblemRecognitionProgressPage generation failure", () => {
  it.each(["question_preparation_time_limit_reached", "question_preparation_wall_time_limit_reached"])("shows both question budgets and preserves explicit recovery for %s", async (code) => {
    failureCode = code;
    questionProviderId = "provider-1";
    generationProgress = { failed_question_ids: [], question_error_codes: {} };
    retryPreparation.mockResolvedValue({ status: "started" });
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(code.includes("_wall_") ? "本次题目准备已达到运行总时限" : "本次题目准备已达到有效处理时限");
    expect(alert).toHaveTextContent("有效处理上限为 1.5 小时");
    expect(alert).toHaveTextContent("总时限为 2.5 小时");
    expect(alert).toHaveTextContent("首次运行后所有等待均计入总时限");
    expect(alert).not.toHaveTextContent("模型响应超时");
    expect(alert).toHaveTextContent("已保存的结果保留");
    expect(alert).toHaveTextContent("已提交的服务商请求可能仍在处理");
    expect(screen.getByRole("button", { name: "人工继续处理" })).toBeEnabled();
    const retry = screen.getByRole("button", { name: "重试失败项" });
    expect(retry).toBeEnabled();
    expect(retryPreparation).not.toHaveBeenCalled();
    expect(manualPreparation).not.toHaveBeenCalled();
    fireEvent.click(retry);
    await waitFor(() => expect(retryPreparation).toHaveBeenCalledExactlyOnceWith({
      taskId: "task-1", jobId: "job-1", recognitionProviderId: "provider-1", expectedWorkflowRevision: 3,
    }));
  });

  it("shows the persisted failed page and refusal without enabling an unacknowledged retry", () => {
    failureCode = "provider_submit_uncertain";
    generationProgress = { provider_failures: [{ code: "provider_recitation_blocked", pages: [8] }] };
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByText(/PDF 第 8 页/)).toHaveTextContent("RECITATION");
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "我了解可能再次计费，确认重试未完成的题目" })).not.toBeChecked();
  });
  it("shows an observed 503 without overwriting it with generic billing copy", () => {
    failureCode = "provider_submit_uncertain";
    generationProgress = { provider_failures: [{ code: "provider_upstream_unavailable", http_status: 503 }] };
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("heading", { name: "模型服务暂时不可用" })).toBeInTheDocument();
    expect(screen.getByText(/HTTP 503/)).toHaveTextContent("重试时会复用已识别资料");
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeDisabled();
  });

  it("keeps uncertain request numbers and completed counts visible after a real-style timeout", () => {
    failureCode = "provider_submit_uncertain";
    generationProgress = {
      total_questions: 7,
      completed_question_ids: ["q1", "q2", "q4", "q5"],
      failed_question_ids: [],
      active_question_ids: ["q3", "q6", "q7"],
      question_labels: { q3: "1.1.20", q6: "1.2.3", q7: "1.2.16" },
      question_error_codes: { q3: "provider_submit_uncertain", q6: "provider_submit_uncertain", q7: "provider_submit_uncertain" },
    };
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByText("已完成 4/7 道大题")).toBeInTheDocument();
    for (const number of ["1.1.20", "1.2.3", "1.2.16"]) {
      expect(screen.getByText(`${number} · 请求未返回结果`)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "我了解可能再次计费，确认重试未完成的题目" })).not.toBeChecked();
  });
  it("shows the actual failed PDF page for a worker recitation error", () => {
    failureCode = "provider_recitation_blocked";
    failedPages = [8];
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByText(/PDF 第 8 页/)).toHaveTextContent("RECITATION");
    expect(screen.getByText(/PDF 第 8 页/)).toHaveTextContent("不代表模型缺少图片能力");
  });
  it("keeps the failed major-question number visible after terminal failure", () => {
    render(
      <MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
        <Routes>
          <Route
            path="/tasks/:taskId/problems/progress"
            element={<ProblemRecognitionProgressPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("以下大题未完成")).toBeInTheDocument();
    expect(screen.getByText("1.1.7 · 模型响应超时")).toBeInTheDocument();
  });
});
