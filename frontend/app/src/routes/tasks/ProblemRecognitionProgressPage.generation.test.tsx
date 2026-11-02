import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";
import type { JobProgress } from "@/types";

let progressOverride: Partial<JobProgress> = {};
let locale = "zh-CN";
beforeEach(() => { progressOverride = {}; locale = "zh-CN"; });

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({ data: [], isError: false, isLoading: false }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
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
      ...progressOverride,
    },
    refetch: vi.fn(),
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale, t: (key: string) => key }),
}));

describe("ProblemRecognitionProgressPage major-question generation", () => {
  it.each(["zh-CN", "en-US"])("keeps the existing stages and two-column activity layout with an empty stage list (%s)", language => {
    locale = language;
    progressOverride = { stage_sequence: [], current_step: "reading_source", completed_steps: 0, total_steps: 0, stage_metrics: {}, active_question_ids: [],
      preparation_counts: { questions: 3, stems: 3, reference_answers: 1, rubrics: 0 } };
    render(<MemoryRouter initialEntries={["/tasks/task-1/problems/progress"]}><Routes>
      <Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} />
    </Routes></MemoryRouter>);
    const savedCounts = screen.getByText(language === "zh-CN" ? /已保存：题干 3 · 标答 1 · 评分标准 0/ : /Saved: question text 3 · Reference answers 1 · Rubrics 0/);
    expect(savedCounts).toBeVisible();
    expect(savedCounts).toHaveClass("mb-2");
    const stages = screen.getByRole("list", { name: "problemProgressStepsLabel" });
    expect(stages).toHaveClass("grid content-start gap-3");
    expect(stages.parentElement).toHaveAttribute("class", "mt-8 grid min-h-0 flex-1 gap-6 md:grid-cols-[210px_minmax(0,1fr)] md:gap-16");
    expect(stages.children).toHaveLength(8);
    expect(screen.getByText("problemProgressStepGenerateSolutions")).toBeVisible();
    expect(screen.getByText("problemProgressStepReadSources")).toBeVisible();
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(screen.queryByText("正在打开人工复核")).not.toBeInTheDocument();
  });
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
