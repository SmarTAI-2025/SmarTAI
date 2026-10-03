import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

const mutateAsync = vi.fn();

describe("GradingPreflightPage regrade mode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mutateAsync.mockResolvedValue({ status: "started" });
    (useTask as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        status: "graded",
        workflow_revision: 8,
        grading_setup_configured: true,
        problem_data: {
          q1: {
            q_id: "q1",
            number: "1",
            type: "short answer",
            criterion: "Award one point for the correct answer.",
            reference_answer: "42",
            review_status: "confirmed",
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
          ready: true,
          blocking_issues: [],
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
      mutateAsync,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens the identity editor when only the student's identity needs review", () => {
    const task = (useTask as unknown as () => any)();
    task.data.student_data.student1.identity_status = "needs_review";
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
      <Routes><Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("link", { name: "Review student identity" })).toHaveAttribute("href", "/tasks/task-1/students/student1?identity=edit");
    expect(screen.getByRole("link", { name: "Review submissions" })).toHaveAttribute("href", "/tasks/task-1/submissions");
  });

  it("opens an unconfirmed answer directly even when it has no recognition flag", () => {
    const task = (useTask as unknown as () => any)();
    task.data.student_data.student1.stu_id = "student / 1";
    task.data.student_data.student1.stu_ans[0] = { q_id: "q 1/2", content: "42", review_status: "pending", flag: [] };
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
      <Routes><Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("link", { name: "Review submissions" })).toHaveAttribute("href", "/tasks/task-1/students/student%20%2F%201?question=q+1%2F2");
    expect(screen.getByRole("button", { name: "Start Grading Anyway" })).toBeEnabled();
  });

  it("keeps identity and answer review destinations separate when both need attention", () => {
    const task = (useTask as unknown as () => any)();
    task.data.student_data.student1.identity_status = "needs_review";
    task.data.student_data.student1.stu_ans[0].review_status = "pending";
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
      <Routes><Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("link", { name: "Review student identity" })).toHaveAttribute("href", "/tasks/task-1/students/student1?identity=edit");
    expect(screen.getByRole("link", { name: "Review submissions" })).toHaveAttribute("href", "/tasks/task-1/students/student1?question=q1");
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

  it("explains a source blocker in a dialog with a direct repair action instead of a disabled button", () => {
    vi.useFakeTimers();
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
          blocking_issues: ["submission_sources_failed"],
          warnings: [],
        },
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(
      <MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
        <Routes>
          <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
          <Route path="/tasks/:taskId/submissions/upload" element={<div>Fix submission uploads</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: "Start Regrading Now" })).toBeEnabled();
    expect(screen.getByText("Some files failed recognition. Review the exact reasons above and resolve them before grading.")).toBeInTheDocument();
    expect(screen.queryByText(/seconds until automatic start/)).not.toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(12_000);
    });
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Start Regrading Now" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Some files failed recognition");
    fireEvent.click(within(dialog).getByRole("button", { name: "Fix submission files" }));
    expect(screen.getByText("Fix submission uploads")).toBeInTheDocument();
  });

  it("blocks Baidu OCR grading with an explicit choose-model message", () => {
    const current = (useGradingSetup as unknown as () => any)();
    (useGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...current,
      data: {
        ...current.data,
        readiness: {
          ready: false,
          blocking_issues: ["ocr_provider_grading_not_supported"],
          warnings: [],
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
        <Routes>
          <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: "Start Regrading Now" })).toBeEnabled();
    expect(screen.getByText(
      "This OCR service does not support grading. Choose a grading model.",
    )).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Start Regrading Now" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Choose a grading model");
    expect(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Fix grading settings" })).toBeInTheDocument();
  });

  it("pauses automatic grading for review warnings but starts with one explicit click", async () => {
    vi.useFakeTimers();
    const task = (useTask as unknown as () => any)();
    task.data.problem_data.q1.review_status = "needs_review";
    task.data.student_data.student1.identity_status = "needs_review";
    task.data.student_data.student1.stu_ans[0].review_status = "pending";
    task.data.student_data.student1.stu_ans[0].flag = ["recognition_needs_review"];
    const setup = (useGradingSetup as unknown as () => any)();
    setup.data.readiness.warnings = ["submission_identities_unresolved", "submission_recognition_needs_review"];
    render(
      <MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
        <Routes>
          <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
          <Route path="/tasks/:taskId/grading/progress" element={<div>Grading started</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText(/You can grade now and keep their review flags/)).toBeInTheDocument();
    expect(screen.getByText(/1 question still needs review/)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(12_000); });
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Start Grading Anyway" })); });
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({ taskId: "task-1", expectedWorkflowRevision: 8 });
    expect(screen.getByText("Grading started")).toBeInTheDocument();
    expect(task.data.student_data.student1.stu_ans[0].review_status).toBe("pending");
    expect(task.data.student_data.student1.identity_status).toBe("needs_review");
  });
});
