import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";
import type { JobProgress } from "@/types";

let failureCode = "provider_timeout";
let failedPages: number[] = [];
let generationProgress: Partial<JobProgress> = {};
beforeEach(() => { failureCode = "provider_timeout"; failedPages = []; generationProgress = {}; });

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({ data: [], isError: false, isLoading: false }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
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
    data: { status: "error", active_job_id: null, last_failed_job_id: "job-1", error: failureCode },
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
  it("shows the locator output limit, the saved stem count and available recovery actions", () => {
    failureCode = "scan_response_truncated";
    generationProgress = { preparation_counts: { stems: 3, questions: 7, reference_answers: 0, rubrics: 0 } };
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("heading", { name: "查找题号所在页时，模型输出被截断" })).toBeVisible();
    expect(screen.getByText(/未能返回完整的候选页码/)).toHaveTextContent("已保存 3 个题干");
    expect(screen.getByText(/未能返回完整的候选页码/)).toHaveTextContent("填写页码范围");
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeVisible();
    expect(screen.getByRole("button", { name: "人工继续处理" })).toBeVisible();
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

  it("explains possible extra model usage when retrying a truncated page search", () => {
    failureCode = "scan_response_truncated";
    generationProgress = {};
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByText(/用同一模型重试时/)).toHaveTextContent("增加等待时间和模型用量");
  });

  it.each([
    ["question_preparation_time_limit_reached", "本次题目处理已达到有效处理时限", "1.5 小时，排队不计时", "已达到有效处理时限"],
    ["question_preparation_wall_time_limit_reached", "本次题目处理已达到总时限", "2.5 小时总时限，自首次开始运行起，之后等待计入", "已达到总时限"],
  ])("shows retained question results and existing recovery actions for %s", (code, title, timing, itemLabel) => {
    failureCode = code;
    generationProgress = { question_error_codes: { q2: code } };
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}>
      <Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("heading", { name: title })).toBeVisible();
    expect(screen.getByText(new RegExp(timing))).toHaveTextContent("已保存的题干、参考答案和评分标准会保留");
    expect(screen.getByText(`1.1.7 · ${itemLabel}`)).toBeVisible();
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "人工继续处理" })).toBeEnabled();
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
