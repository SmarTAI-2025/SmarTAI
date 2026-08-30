import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({ data: [], isError: false, isLoading: false }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useTask: () => ({
    data: {
      task_id: "task-1",
      status: "error",
      workflow_revision: 3,
      last_failed_job_id: "job-1",
      error: "provider_timeout",
    },
    error: null,
    isFetching: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({
    data: { status: "error", active_job_id: null },
    error: null,
    isFetching: false,
    progress: {
      phase: "error",
      error_detail: "provider_timeout",
      question_labels: { q2: "1.1.7" },
      failed_question_ids: ["q2"],
      question_error_codes: { q2: "provider_timeout" },
      messages: [],
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
