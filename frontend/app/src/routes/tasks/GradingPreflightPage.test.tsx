import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GradingPreflightPage } from "./GradingPreflightPage";

vi.mock("@/api/hooks", () => ({
  useGradingSetup: vi.fn(),
  useStartGrading: vi.fn(),
  useTask: vi.fn(),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => <div>Workflow steps</div>,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({
    locale: "en-US",
    t: (key: string) => key,
  }),
}));

const { useGradingSetup, useStartGrading, useTask } = await import("@/api/hooks");

describe("GradingPreflightPage regrade mode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useTask as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        status: "graded",
        grading_setup_configured: true,
        problem_data: {
          q1: {
            q_id: "q1",
            number: "1",
            type: "short answer",
            criterion: "Award one point for the correct answer.",
            reference_answer: "42",
          },
        },
        student_data: {
          student1: {
            stu_id: "student1",
            stu_name: "Student",
            identity_status: "matched",
            stu_ans: [{
              q_id: "q1",
              content: "42",
              review_status: "confirmed",
              flag: [],
            }],
          },
        },
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    (useGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        task_status: "graded",
        workflow_revision: 8,
        configured: true,
        grading_setup: {
          schema_version: 1,
          selected_provider_ids: ["provider-1"],
          primary_provider_id: "provider-1",
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
        suggested_setup: null,
        grading_setup_fingerprint: "setup-2",
        grading_setup_updated_at: 2,
        available_experts: [{
          provider_id: "provider-1",
          provider_type: "gemini",
          model: "gemini-3.1-flash-lite-preview",
          display_name: "Calculus grader",
          enabled: true,
          scope: "owner",
          is_shared: false,
          editable: true,
          max_concurrent: 1,
          rpm: 10,
        }],
        knowledge: {
          scope_options: ["none", "all_task_docs"],
          task_doc_count: 0,
          task_docs: [],
        },
        readiness: {
          ready: false,
          blocking_issues: ["invalid_state"],
          warnings: [],
        },
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    (useStartGrading as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      error: null,
      isPending: false,
      mutateAsync: vi.fn(),
    });
  });

  it("treats a completed task as a startable regrade after setup is saved", () => {
    render(
      <MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
        <Routes>
          <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("Regrading is about to start")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start Regrading Now" })).toBeEnabled();
    expect(screen.queryByText("Historical configuration")).not.toBeInTheDocument();
  });
});
