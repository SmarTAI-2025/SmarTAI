import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { GradingProgressPage } from "./GradingProgressPage";

let taskStatus = "grading";

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
    expect(screen.getByRole("alert")).toHaveTextContent("grading_failed");
    expect(screen.getByRole("alert")).toHaveTextContent("run-1");
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
    expect(screen.getByRole("alert")).toHaveTextContent("provider_timeout");
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

    fireEvent.click(screen.getByRole("button", { name: "Refresh task state" }));

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
