import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FinalResultsWorkspacePage } from "./FinalResultsWorkspacePage";

const useTaskMock = vi.hoisted(() => vi.fn());
const useTaskResultMock = vi.hoisted(() => vi.fn());
const useTaskFinalizationMock = vi.hoisted(() => vi.fn());

vi.mock("@/api/hooks/tasks", () => ({
  useTask: useTaskMock,
  useTaskResult: useTaskResultMock,
  useTaskFinalization: useTaskFinalizationMock,
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US", t: (key: string) => key }),
}));

vi.mock("@/routes/tasks/results/VisualizationAnalysisPage", () => ({
  VisualizationAnalysisPage: () => <div>visualization-target</div>,
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-pathname">{location.pathname}</output>;
}

function TestTree() {
  return (
    <MemoryRouter initialEntries={["/tasks/task-1/results/visualizations"]}>
      <LocationProbe />
      <Routes>
        <Route path="/tasks/:taskId/results/visualizations" element={<FinalResultsWorkspacePage />} />
        <Route path="/tasks/:taskId/submissions" element={<div>submission-redirect-target</div>} />
      </Routes>
    </MemoryRouter>
  );
}

const staleTask = {
  task_id: "task-1",
  name: "Cached task",
  status: "submissions_ready",
};

const gradedTask = {
  ...staleTask,
  name: "Fresh task",
  status: "graded",
};

const finalizedTask = {
  ...gradedTask,
  status: "finalized",
};

const finalization = {
  task_id: "task-1",
  task_status: "graded",
  workflow_revision: 1,
  ready_for_confirmation: true,
  required_review_count: 0,
  confirmed_required_count: 0,
  remaining_review_count: 0,
  remaining_reviews: [],
  final_result_version: 0,
  final_result_dirty: false,
  analysis_status: "not_generated",
  available_result_versions: 0,
};

describe("FinalResultsWorkspacePage task-status refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("scrollTo", vi.fn());
    useTaskResultMock.mockReturnValue({
      data: { status: "completed", task_id: "task-1", results: [] },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    useTaskFinalizationMock.mockReturnValue({
      data: finalization,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
  });

  it("waits for a fresh task status instead of redirecting the first visualization visit", async () => {
    useTaskMock.mockReturnValue({
      data: staleTask,
      isLoading: false,
      isFetching: true,
      isError: false,
      refetch: vi.fn(),
    });
    const view = render(<TestTree />);

    expect(screen.getByTestId("location-pathname")).toHaveTextContent("/tasks/task-1/results/visualizations");
    expect(screen.getByRole("heading", { name: "Loading final results…" })).toBeInTheDocument();
    expect(screen.queryByText("submission-redirect-target")).not.toBeInTheDocument();

    useTaskMock.mockReturnValue({
      data: gradedTask,
      isLoading: false,
      isFetching: false,
      isError: false,
      refetch: vi.fn(),
    });
    view.rerender(<TestTree />);

    expect(await screen.findByText("visualization-target")).toBeInTheDocument();
    expect(screen.getByTestId("location-pathname")).toHaveTextContent("/tasks/task-1/results/visualizations");
    expect(screen.queryByText("submission-redirect-target")).not.toBeInTheDocument();
  });

  it("still redirects after the fresh task status confirms results are unavailable", async () => {
    useTaskMock.mockReturnValue({
      data: staleTask,
      isLoading: false,
      isFetching: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<TestTree />);

    expect(await screen.findByText("submission-redirect-target")).toBeInTheDocument();
    expect(screen.getByTestId("location-pathname")).toHaveTextContent("/tasks/task-1/submissions");
  });

  it("reports automatic cleanup retry without exposing a manual retry action", async () => {
    useTaskMock.mockReturnValue({
      data: finalizedTask,
      isLoading: false,
      isFetching: false,
      isError: false,
      refetch: vi.fn(),
    });
    useTaskFinalizationMock.mockReturnValue({
      data: {
        ...finalization,
        task_status: "finalized",
        final_result_version: 1,
        final_result_updated_at: 1_700_000_000,
        source_cleanup: {
          status: "retrying",
          total_count: 2,
          deleted_count: 0,
          pending_count: 2,
          retrying_count: 1,
          pending_bytes: 2_048,
          retrying_bytes: 1_024,
          automatic_retry: true,
        },
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<TestTree />);

    expect(await screen.findByText(/Automatic retry is in progress; no manual action is needed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry cleanup/i })).not.toBeInTheDocument();
  });
});
