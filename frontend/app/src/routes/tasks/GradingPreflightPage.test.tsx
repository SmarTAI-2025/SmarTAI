import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { act, fireEvent, render as rtlRender, screen, within } from "@testing-library/react";
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
  it.each(["grading_setup", "graded", "finalized", "error"])("starts a fresh countdown after %s and resets it on re-entry", async (status) => {
    vi.useFakeTimers();
    const task = (useTask as unknown as () => any)();
    Object.assign(task.data, { status, grading_job_id: "old-run", last_failed_job_id: status === "error" ? "old-run" : null });
    const page = <MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}><Routes>
      <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
      <Route path="/tasks/:taskId/grading/progress" element={<div>New run progress</div>} />
    </Routes></MemoryRouter>;
    const first = render(page);
    act(() => { vi.advanceTimersByTime(9_000); });
    expect(mutateAsync).not.toHaveBeenCalled();
    first.unmount();
    render(page);
    act(() => { vi.advanceTimersByTime(9_000); });
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(1_000); });
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ requestId: expect.any(String), expectedWorkflowRevision: 8 }));
    expect(screen.getByText("New run progress")).toBeInTheDocument();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
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
            type: "concept",
            stem: "What is six times seven?",
            max_score: 1,
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
    vi.restoreAllMocks();
  });

  it("opens the identity editor when only the student's identity needs review", () => {
    const task = (useTask as unknown as () => any)();
    task.data.student_data.student1.identity_status = "needs_review";
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
      <Routes><Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("link", { name: "Review student identity" })).toHaveAttribute("href", "/tasks/task-1/students/student1?identity=edit");
    expect(screen.queryByRole("link", { name: "Review submissions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Review question materials" })).not.toBeInTheDocument();
  });

  it("opens an unconfirmed answer directly even when it has no recognition flag", () => {
    const task = (useTask as unknown as () => any)();
    task.data.student_data.student1.stu_id = "student / 1";
    task.data.student_data.student1.stu_ans[0] = { q_id: "q 1/2", content: "42", review_status: "pending", flag: [] };
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}>
      <Routes><Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} /></Routes>
    </MemoryRouter>);
    expect(screen.getByRole("link", { name: "Review submissions" })).toHaveAttribute("href", "/tasks/task-1/students/student%20%2F%201?question=q+1%2F2");
    expect(screen.getByRole("button", { name: "Start Regrading Now" })).toBeEnabled();
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

  it.each([
    { name: "ordinary pending confirmations", problem: {}, answer: {} },
    ...[0.24, 0.59, 0.6].flatMap(confidence => [
      { name: `question confidence ${confidence}`, problem: { preparation_issues: [{
        issue_id: "confidence-q1", q_id: "q1", field: "stem", code: "low_confidence",
        severity: "warning", status: "open", details: { confidence },
      }] }, answer: {} },
      { name: `answer confidence ${confidence}`, problem: {}, answer: { recognition_confidence: confidence } },
    ]),
    { name: "missing optional reference answer", problem: { reference_answer: "" }, answer: {} },
    { name: "external annotations", problem: {}, answer: { flag: ["external_annotation_present"] } },
  ])("keeps the countdown for $name and never confirms inputs implicitly", async ({ problem, answer }) => {
    vi.useFakeTimers();
    const task = (useTask as unknown as () => any)();
    task.data.status = "submissions_ready";
    task.data.problem_data.q1.review_status = "needs_review";
    task.data.student_data.student1.stu_ans[0].review_status = "pending";
    task.data.student_data.student1.stu_ans[0].flag = ["recognition_needs_review"];
    Object.assign(task.data.problem_data.q1, problem);
    Object.assign(task.data.student_data.student1.stu_ans[0], answer);
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}><Routes>
      <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
      <Route path="/tasks/:taskId/grading/progress" element={<div>Grading started</div>} />
    </Routes></MemoryRouter>);
    expect(screen.getByText(/Pending confirmation does not block/)).toBeInTheDocument();
    expect(screen.getByText(/1 question awaits confirmation/)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(9_000); });
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(1_000); });
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({ taskId: "task-1", expectedWorkflowRevision: 8, requestId: expect.any(String) });
    expect(task.data.student_data.student1.stu_ans[0].review_status).toBe("pending");
    expect(task.data.problem_data.q1.review_status).toBe("needs_review");
  });

  it.each(["failed recognition", "missing stem", "missing rubric", "assignment conflict"])("does not auto-start or manually bypass %s", async (blocker) => {
    vi.useFakeTimers();
    const task = (useTask as unknown as () => any)();
    task.data.status = "submissions_ready";
    if (blocker === "failed recognition") task.data.student_data.student1.stu_ans[0].flag = ["recognition_failed"];
    if (blocker === "missing stem") task.data.problem_data.q1.stem = "";
    if (blocker === "missing rubric") task.data.problem_data.q1.criterion = "";
    if (blocker === "assignment conflict") Object.assign(task.data.student_data.student1.stu_ans[0], {
      flag: ["answer_assignment_conflict"], review_status: "pending",
    });
    render(<MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}><Routes>
      <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
    </Routes></MemoryRouter>);
    await act(async () => { vi.advanceTimersByTime(12_000); });
    fireEvent.click(screen.getByRole("button", { name: "Start Grading Now" }));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("keeps counting when advice arrives, but cancels when a blocker arrives", async () => {
    vi.useFakeTimers();
    const task = (useTask as unknown as () => any)();
    task.data.status = "submissions_ready";
    const client = new QueryClient();
    const page = () => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/tasks/task-1/grading/preflight"]}><Routes>
      <Route path="/tasks/:taskId/grading/preflight" element={<GradingPreflightPage />} />
      <Route path="/tasks/:taskId/grading/progress" element={<div>Grading started</div>} />
    </Routes></MemoryRouter></QueryClientProvider>;
    const view = rtlRender(page());
    act(() => { vi.advanceTimersByTime(4_000); });
    Object.assign(task.data.student_data.student1.stu_ans[0], { review_status: "pending", recognition_confidence: 0.24 });
    // Rerender the same mounted page, as a task query refresh would.
    view.rerender(page());
    expect(screen.getByText(/6 seconds until automatic start/)).toBeInTheDocument();
    task.data.student_data.student1.stu_ans[0].flag = ["recognition_failed"];
    view.rerender(page());
    expect(screen.queryByText(/seconds until automatic start/)).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(10_000); });
    expect(mutateAsync).not.toHaveBeenCalled();
  });
});

function render(ui: ReactElement) { return rtlRender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>); }
