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
