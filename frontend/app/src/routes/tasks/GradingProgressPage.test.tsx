import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { GradingProgressPage } from "./GradingProgressPage";

let taskStatus = "grading";
vi.mock("@/api/tasks", async importOriginal => ({ ...await importOriginal<typeof import("@/api/tasks")>(), continueTaskRun: vi.fn() }));
const { continueTaskRun } = await import("@/api/tasks");

vi.mock("@/api/hooks/tasks", () => ({
  useStartGrading: vi.fn(),
  useTask: vi.fn(),
}));

vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: vi.fn(),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({
    locale: "en-US",
    t: (key: string) => key,
  }),
}));

const { useStartGrading, useTask } = await import("@/api/hooks/tasks");
const { useTaskProgress } = await import("@/hooks/useTaskProgress");

function renderProgress() {
  return render(
    <MemoryRouter initialEntries={["/tasks/task-1/grading/progress"]}>
      <Routes>
        <Route path="/tasks/:taskId/grading/progress" element={<GradingProgressPage />} />
        <Route path="/tasks/:taskId/review" element={<div>Review overview</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("GradingProgressPage completion routing", () => {
  it("withdraws a cached model estimate when polling fails", () => {
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { status: "grading", problem_count: 7, student_count: 2 },
      progress: { total_students: 2, total_questions: 7, completed_units: 3, messages: [], active: [],
        model_progress: [{ id: "model-1", total: 14, succeeded: 3, failed: 0, retrying: 0 }], model_eta_seconds: 220 },
      percent: 21, isError: true, refetch: vi.fn(),
    });
    renderProgress();
    expect(screen.getByText(/Estimated remaining Estimating/)).toBeVisible();
  });
  it("keeps the factual percent and model rows during a slow model retry", () => {
    const models = ["Gemini", "Tsinghua"].map((label, i) => ({ id: `model-${i + 1}`, label, total: 14,
      succeeded: i ? 1 : 14, failed: 0, cancelled: 0, responding: i ? 12 : 0, processing: 0, queued: 0, retrying: i ? 1 : 0, completion_times: [] }));
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { status: "grading", problem_count: 7, student_count: 2 },
      progress: { phase: "grading", total_students: 2, total_questions: 7, completed_units: 1,
        messages: [], active: [], model_progress: models, model_eta_seconds: null,
        model_waits: [{ model: "Tsinghua", reason: "provider_rate_limited", attempt: 2, max_attempts: 3, retry_at: 1 }] },
      percent: 7, refetch: vi.fn(),
    });
    renderProgress();
    expect(screen.getByText(/processed 14 \/ 14/)).toBeVisible();
    expect(screen.getByText(/processed 1 \/ 14/)).toBeVisible();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "7");
    expect(screen.getByText(/Estimated remaining Waiting for model retry/)).toBeVisible();
  });
  it.each([1, 3])("does not extrapolate hours from %s completed responses without a recent representative rate", (completed) => {
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { status: "grading", problem_count: 7, student_count: 2 },
      progress: { total_students: 2, total_questions: 7, completed_units: completed,
        started_at: Date.now() / 1000 - 600,
        messages: [{ message: "Completed one grading unit.", ts: Date.now() / 1000 - 120, level: "info" }], active: [] },
      percent: 7, refetch: vi.fn(),
    });
    renderProgress();
    expect(screen.getByText(/Estimated remaining Estimating/)).toBeInTheDocument();
    expect(screen.queryByText(/about .* hr/)).not.toBeInTheDocument();
  });
  it("still estimates a batch with several recently completed responses", () => {
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { status: "grading", problem_count: 7, student_count: 2 },
      progress: { total_students: 2, total_questions: 7, completed_units: 3,
        started_at: Date.now() / 1000 - 60,
        messages: [{ message: "Completed one grading unit.", ts: Date.now() / 1000, level: "info" }], active: [] },
      percent: 21, refetch: vi.fn(),
    });
    renderProgress();
    expect(screen.getByText(/Estimated remaining about 4 min/)).toBeInTheDocument();
  });
  it("withdraws an estimate when unchanged polling data becomes stale", () => {
    const now = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(now);
    const progress = { total_students: 2, total_questions: 7, completed_units: 3,
      started_at: now / 1000 - 60,
      messages: [{ message: "Completed one grading unit.", ts: now / 1000, level: "info" }], active: [] };
    let updatedAt = now;
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      data: { status: "grading", problem_count: 7, student_count: 2 },
      progress, percent: 21, dataUpdatedAt: updatedAt, refetch: vi.fn(),
    }));
    try {
      const view = renderProgress();
      expect(screen.getByText(/Estimated remaining about 4 min/)).toBeInTheDocument();
      clock.mockReturnValue(now + 61_000);
      updatedAt = now + 61_000;
      view.rerender(<MemoryRouter initialEntries={["/tasks/task-1/grading/progress"]}>
        <Routes><Route path="/tasks/:taskId/grading/progress" element={<GradingProgressPage />} /></Routes>
      </MemoryRouter>);
      expect(screen.getByText(/Estimated remaining Estimating/)).toBeInTheDocument();
    } finally {
      clock.mockRestore();
    }
  });
  beforeEach(() => {
    vi.clearAllMocks();
    taskStatus = "grading";
    (useTask as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      data: {
        task_id: "task-1",
        status: taskStatus,
        workflow_revision: 4,
        grading_job_id: "run-1",
        last_failed_job_id: taskStatus === "error" ? "run-1" : null,
        grading_setup_configured: true,
        problem_count: 1,
        student_count: 1,
      },
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    }));
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      data: {
        task_id: "task-1",
        status: taskStatus,
        problem_count: 1,
        student_count: 1,
      },
      progress: null,
      percent: taskStatus === "graded" ? 100 : 0,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    }));
    (useStartGrading as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      error: null,
      isPending: false,
      mutateAsync: vi.fn(),
      reset: vi.fn(),
    });
  });

  it("automatically opens review when an active grading run becomes completed", async () => {
    const view = renderProgress();
    expect(screen.getByRole("heading", { name: "Grading in Progress" })).toBeInTheDocument();

    taskStatus = "graded";
    view.rerender(
      <MemoryRouter initialEntries={["/tasks/task-1/grading/progress"]}>
        <Routes>
          <Route path="/tasks/:taskId/grading/progress" element={<GradingProgressPage />} />
          <Route path="/tasks/:taskId/review" element={<div>Review overview</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Review overview")).toBeInTheDocument();
  });

  it("shows saved grades separately from aggregation and hands a paused run to review", async () => {
    taskStatus = "grading_paused";
    vi.mocked(continueTaskRun).mockResolvedValue({ status: "manual_review", job_id: "run-1" });
    vi.mocked(useTaskProgress).mockReturnValue({
      data: { status: "grading_paused", workflow_revision: 5, problem_count: 14, student_count: 1 },
      progress: { phase: "paused", total_students: 1, total_questions: 14, completed_units: 0,
        saved_units: 5, messages: [], active: [] },
      percent: 0, refetch: vi.fn(),
    } as unknown as ReturnType<typeof useTaskProgress>);
    renderProgress();
    expect(screen.getByText(/Saved AI grades 5 \/ 14 responses · Aggregated 0 \/ 14/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Continue manually" }));
    expect(await screen.findByText("Review overview")).toBeVisible();
    expect(continueTaskRun).toHaveBeenCalledWith("task-1", "run-1", 5, true);
  });

  it("shows immediate manual handoff feedback and restores the retry action after a slow failure", async () => {
    taskStatus = "grading_paused";
    vi.mocked(useTaskProgress).mockReturnValue({
      data: { status: "grading_paused", workflow_revision: 5, problem_count: 1, student_count: 1 },
      progress: { phase: "paused", total_students: 1, total_questions: 1, completed_units: 0, messages: [], active: [] },
      percent: 0, refetch: vi.fn(),
    } as unknown as ReturnType<typeof useTaskProgress>);
    let reject!: (error: unknown) => void;
    vi.mocked(continueTaskRun).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    renderProgress();
    fireEvent.click(screen.getByRole("button", { name: "Continue manually" }));
    expect(screen.getByText("Opening manual review")).toBeVisible();
    expect(screen.getByRole("button", { name: "Opening…" })).toBeDisabled();
    reject(new Error("Recovery failed"));
    expect(await screen.findByText("Could not open review. Refresh and try again.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Continue manually" })).toBeEnabled();
  });

  it("shows the persisted grading failure code instead of a stale done state", () => {
    taskStatus = "error";
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        status: "error",
        error: "grading_failed",
        grading_job_id: "run-1",
        problem_count: 1,
        student_count: 1,
      },
      progress: {
        phase: "error",
        current_step: "grading",
        error_detail: "grading_failed",
        messages: [],
      },
      percent: 0,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });

    renderProgress();

    expect(screen.getByRole("alert")).toHaveTextContent("This grading run did not finish");
    expect(screen.getByRole("alert")).not.toHaveTextContent("grading_failed");
    expect(screen.getByRole("alert")).not.toHaveTextContent("run-1");
  });

  it("renders the classified provider cause from the grading run", () => {
    taskStatus = "error";
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        status: "error",
        error: "provider_timeout",
        grading_job_id: "run-timeout",
        problem_count: 1,
        student_count: 1,
      },
      progress: {
        phase: "error",
        current_step: "grading",
        error_detail: "provider_timeout",
        messages: [],
      },
      percent: 0,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });

    renderProgress();

    expect(screen.getByRole("alert")).toHaveTextContent("The model took too long to respond");
    expect(screen.getByRole("alert")).not.toHaveTextContent("provider_timeout");
    expect(screen.getByRole("alert")).not.toHaveTextContent("grading_failed");
  });

  it("resets a stale retry conflict before refetching task state", () => {
    taskStatus = "error";
    let retryError: unknown = new APIError(409, "workflow_revision_conflict", {
      detail: { code: "workflow_revision_conflict" },
    });
    const reset = vi.fn(() => {
      retryError = null;
    });
    const taskRefetch = vi.fn();
    const progressRefetch = vi.fn();
    (useTask as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      data: {
        task_id: "task-1",
        status: "error",
        workflow_revision: 4,
        grading_job_id: "run-1",
        last_failed_job_id: "run-1",
        grading_setup_configured: true,
        problem_count: 1,
        student_count: 1,
      },
      isError: false,
      isFetching: false,
      refetch: taskRefetch,
    }));
    (useTaskProgress as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        status: "error",
        error: "grading_failed",
        problem_count: 1,
        student_count: 1,
      },
      progress: {
        phase: "error",
        current_step: "grading",
        error_detail: "grading_failed",
        messages: [],
      },
      percent: 0,
      isError: false,
      isFetching: false,
      refetch: progressRefetch,
    });
    (useStartGrading as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      error: retryError,
      isPending: false,
      mutateAsync: vi.fn(),
      reset,
    }));

    const view = renderProgress();
    expect(screen.getByRole("alert")).toHaveTextContent("The task state has changed");

    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));

    expect(reset).toHaveBeenCalledTimes(1);
    expect(taskRefetch).toHaveBeenCalledTimes(1);
    expect(progressRefetch).toHaveBeenCalledTimes(1);
    expect(reset.mock.invocationCallOrder[0]).toBeLessThan(taskRefetch.mock.invocationCallOrder[0]);
    expect(reset.mock.invocationCallOrder[0]).toBeLessThan(progressRefetch.mock.invocationCallOrder[0]);

    view.rerender(
      <MemoryRouter initialEntries={["/tasks/task-1/grading/progress"]}>
        <Routes>
          <Route path="/tasks/:taskId/grading/progress" element={<GradingProgressPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This grading run did not finish");
  });

  it("honors the exact recovery link for a grading readiness conflict", () => {
    taskStatus = "error";
    (useStartGrading as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      error: new APIError(409, "submission_identities_unresolved", {
        detail: { code: "submission_identities_unresolved" },
      }),
      isPending: false,
      mutateAsync: vi.fn(),
      reset: vi.fn(),
    });

    renderProgress();

    expect(screen.getByRole("link", { name: "Review student identities" }))
      .toHaveAttribute("href", "/tasks/task-1/submissions");
    expect(screen.getByRole("alert")).not.toHaveTextContent("The task state has changed");
  });
});
