import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({ data: [], isError: false, isLoading: false }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useTask: () => ({
    data: { task_id: "task-1", status: "extracting_problems" },
    error: null,
    isFetching: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({
    data: { status: "extracting_problems", active_job_id: "job-1" },
    error: null,
    isFetching: false,
    percent: 42,
    progress: {
      job_id: "job-1",
      phase: "parsing",
      workflow: "question_preparation",
      stage_sequence: [
        "validating_sources",
        "extracting_questions",
        "aligning_uploaded_materials",
        "generating_solutions",
        "aligning_rubrics",
        "preparing_programming_tests",
        "detecting_conflicts",
        "committing_question_packages",
      ],
      current_step: "generating_solutions",
      completed_steps: 3,
      total_steps: 8,
      stage_metrics: {
        solution_total_questions: 7,
        solution_completed_questions: 3,
        solution_failed_questions: 0,
      },
      question_labels: { q4: "1.1.29", q5: "1.1.31" },
      active_question_ids: ["q4", "q5"],
      failed_question_ids: [],
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

describe("ProblemRecognitionProgressPage major-question generation", () => {
  it("shows factual completed and active major-question counts", () => {
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

    expect(screen.getByText("已完成 3/7 道大题")).toBeInTheDocument();
    expect(screen.getByText("正在处理：1.1.29、1.1.31")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42");
    expect(screen.queryByText(/\(a\)|\(b\)/)).not.toBeInTheDocument();
  });
});
