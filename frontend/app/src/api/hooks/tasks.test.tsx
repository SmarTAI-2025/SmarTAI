import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CorrectionReviewResponse,
  Task,
  TaskFinalizationResponse,
  GenerateResultArtifactsResponse,
  TaskStateSnapshot,
} from "@/types";
import { taskKeys } from "./keys";
import {
  useGenerateTaskResultArtifacts,
  useStartGrading,
  useUpdateCorrectionReview,
} from "./tasks";

vi.mock("@/api/tasks", () => ({
  generateTaskResultArtifacts: vi.fn(),
  startGrading: vi.fn(),
  updateCorrectionReview: vi.fn(),
}));

const tasksApi = await import("@/api/tasks");

describe("task report and review cache", () => {
  beforeEach(() => vi.clearAllMocks());

  it("updates the report, task, and revision together after generation", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData<Task>(taskKeys.detail("task-1"), {
      task_id: "task-1",
      status: "graded",
      workflow_revision: 7,
    } as Task);
    const response: GenerateResultArtifactsResponse = {
      status: "ok",
      unchanged: false,
      task_id: "task-1",
      task_status: "review_confirmed",
      workflow_revision: 8,
      ready_for_confirmation: true,
      required_review_count: 0,
      confirmed_required_count: 0,
      remaining_review_count: 0,
      remaining_reviews: [],
      final_result_version: 1,
      final_result_dirty: false,
      analysis_status: "ready",
      available_result_versions: 1,
      artifacts: { task_id: "task-1", current_result_version: 1, analysis_status: "ready", versions: [] },
    };
    vi.mocked(tasksApi.generateTaskResultArtifacts).mockResolvedValue(response);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useGenerateTaskResultArtifacts(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ taskId: "task-1", expectedWorkflowRevision: 7 });
    });

    expect(client.getQueryData<Task>(taskKeys.detail("task-1"))).toMatchObject({
      status: "review_confirmed",
      workflow_revision: 8,
    });
    expect(client.getQueryData(taskKeys.finalization("task-1"))).toMatchObject({ workflow_revision: 8 });
    expect(client.getQueryData(taskKeys.artifacts("task-1"))).toEqual(response.artifacts);
  });

  it("removes a confirmed response from the cached finalization queue", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData<Task>(taskKeys.detail("task-1"), {
      task_id: "task-1",
      status: "finalized",
      workflow_revision: 7,
    } as Task);
    client.setQueryData<TaskFinalizationResponse>(taskKeys.finalization("task-1"), {
      task_id: "task-1",
      task_status: "graded",
      workflow_revision: 7,
      ready_for_confirmation: false,
      required_review_count: 1,
      confirmed_required_count: 0,
      remaining_review_count: 1,
      remaining_reviews: [{ student_id: "student-1", q_id: "q1", reasons: ["review"], confirmed: false }],
      final_result_version: 0,
      final_result_dirty: true,
      analysis_status: "not_generated",
      available_result_versions: 0,
    });
    vi.mocked(tasksApi.updateCorrectionReview).mockResolvedValue({
      status: "ok",
      unchanged: false,
      student_id: "student-1",
      q_id: "q1",
      workflow_revision: 8,
      correction: { q_id: "q1", review_status: "confirmed" },
    } as CorrectionReviewResponse);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useUpdateCorrectionReview(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        taskId: "task-1",
        studentId: "student-1",
        qId: "q1",
        expected_workflow_revision: 7,
        teacher_score: 8,
        teacher_comment: "Reviewed",
        confirm: true,
      });
    });

    expect(client.getQueryData<TaskFinalizationResponse>(taskKeys.finalization("task-1"))).toMatchObject({
      task_status: "graded",
      workflow_revision: 8,
      ready_for_confirmation: true,
      confirmed_required_count: 1,
      remaining_review_count: 0,
      remaining_reviews: [],
    });
    expect(client.getQueryData<Task>(taskKeys.detail("task-1"))).toMatchObject({
      status: "graded",
      workflow_revision: 8,
    });
  });
});

describe("grading run cache", () => {
  beforeEach(() => vi.clearAllMocks());

  it("marks a regrade as active before progress routing reads stale completed data", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData<Task>(taskKeys.detail("task-1"), {
      task_id: "task-1",
      status: "graded",
      grading_job_id: "old-run",
    } as Task);
    client.setQueryData<TaskStateSnapshot>(taskKeys.state("task-1"), {
      task_id: "task-1",
      status: "graded",
      grading_job_id: "old-run",
    } as TaskStateSnapshot);
    vi.mocked(tasksApi.startGrading).mockResolvedValue({
      status: "started",
      task_id: "task-1",
      job_id: "new-run",
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useStartGrading(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        taskId: "task-1",
        expectedWorkflowRevision: 1,
      });
    });

    expect(client.getQueryData<Task>(taskKeys.detail("task-1"))).toMatchObject({
      status: "grading",
      grading_job_id: "new-run",
    });
    expect(client.getQueryData<TaskStateSnapshot>(taskKeys.state("task-1"))).toMatchObject({
      status: "grading",
      grading_job_id: "new-run",
    });
  });
});
