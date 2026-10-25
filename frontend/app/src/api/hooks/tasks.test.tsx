import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CorrectionReviewResponse,
  Task,
  TaskFinalizationResponse,
  GenerateResultArtifactsResponse,
  TaskStateSnapshot,
  TaskResultResponse,
} from "@/types";
import { taskKeys } from "./keys";
import {
  useTask,
  useUpdateStudentAnswer,
  useGenerateTaskResultArtifacts,
  useStartGrading,
  useUpdateCorrectionReview,
} from "./tasks";

vi.mock("@/api/tasks", () => ({
  getTask: vi.fn(),
  updateStudentAnswer: vi.fn(),
  generateTaskResultArtifacts: vi.fn(),
  startGrading: vi.fn(),
  updateCorrectionReview: vi.fn(),
}));

const tasksApi = await import("@/api/tasks");

describe("task report and review cache", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps concise AI feedback when a review response carries the stored legacy text", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const steps = [{ step_no: 1, desc: "Missing the required condition.", score: 1, is_correct: false }];
    const experts = [{ provider: "Model A", score: 1, max_score: 2, confidence: .9, comment: "Summary", steps }];
    client.setQueryData(taskKeys.result("task-1"), { results: [{ student_id: "student-1", corrections: [
      { q_id: "q1", score: 1, provisional_score: 1, steps, expert_results: experts },
      { q_id: "q2", score: 2, steps: [] },
    ] }] });
    vi.mocked(tasksApi.updateCorrectionReview).mockResolvedValue({ status: "ok", unchanged: false,
      student_id: "student-1", q_id: "q1", workflow_revision: 8,
      correction: { result_id: "review-copy", q_id: "q1", score: 2, provisional_score: 1,
        type: "reasoning", max_score: 2, confidence: .9, comment: "Summary", requires_human_review: false,
        synthesis_method: "single", review_reasons: [],
        teacher_score: 2, teacher_comment: "Condition is implicit.", review_status: "confirmed",
        steps: [{ ...steps[0], desc: "Rubric\nRubric\nMissing the required condition." }],
        expert_results: [{ ...experts[0], steps: [] }],
      },
    } as CorrectionReviewResponse);
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useUpdateCorrectionReview(), { wrapper });
    await act(async () => { await result.current.mutateAsync({ taskId: "task-1", studentId: "student-1", qId: "q1",
      expected_workflow_revision: 7, teacher_score: 2, teacher_comment: "Condition is implicit.", confirm: true }); });
    const corrections = client.getQueryData<TaskResultResponse>(taskKeys.result("task-1"))!.results![0].corrections;
    expect(corrections[0]).toMatchObject({ result_id: "review-copy", score: 2, provisional_score: 1,
      teacher_score: 2, teacher_comment: "Condition is implicit.", review_status: "confirmed", steps, expert_results: experts });
    expect(corrections[1]).toEqual({ q_id: "q2", score: 2, steps: [] });
  });

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
      task_status: "graded",
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
      status: "graded",
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


describe("answer review revision fences", () => {
  it("keeps a newer saved task when an older task read arrives late", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const oldTask = { task_id: "late", workflow_revision: 1, student_data: {} } as Task;
    const currentTask = { ...oldTask, workflow_revision: 2 };
    let resolve!: (task: Task) => void;
    vi.mocked(tasksApi.getTask).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useTask("late"), { wrapper });
    await act(async () => { client.setQueryData(taskKeys.detail("late"), currentTask); resolve(oldTask); });
    expect(client.getQueryData(taskKeys.detail("late"))).toEqual(currentTask);
    await waitFor(() => expect(result.current.data?.workflow_revision).toBe(2));
  });

  it("updates both conflicting answers by stable identity and ignores an older save", async () => {
    const client = new QueryClient();
    const first = { answer_id: "a1", q_id: "q1", number: "1", type: "short", content: "first", flag: [] };
    const second = { answer_id: "a2", q_id: "q2", number: "2", type: "short", content: "second", flag: [] };
    client.setQueryData<Partial<Task>>(taskKeys.detail("review"), { task_id: "review", workflow_revision: 1,
      student_data: { S1: { stu_id: "S1", stu_name: "Synthetic", stu_ans: [first, second] } } });
    const conflict = [first, { ...second, q_id: "q1" }].map(answer => ({ ...answer, flag: ["answer_assignment_conflict"] }));
    vi.mocked(tasksApi.updateStudentAnswer).mockResolvedValueOnce({ status: "ok", stu_id: "S1", q_id: "q2",
      answer: conflict[1], answers: conflict, workflow_revision: 2 });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useUpdateStudentAnswer(), { wrapper });
    const variables = { taskId: "review", studentId: "S1", qId: "q2", answerId: "a2", targetQId: "q1", expectedWorkflowRevision: 1 };
    await act(async () => { await result.current.mutateAsync(variables); });
    const saved = client.getQueryData<Task>(taskKeys.detail("review"))!;
    expect(saved.student_data.S1.stu_ans).toEqual(conflict);
    vi.mocked(tasksApi.updateStudentAnswer).mockResolvedValueOnce({ status: "ok", stu_id: "S1", q_id: "q2", answer: second, workflow_revision: 1 });
    await act(async () => { await result.current.mutateAsync(variables); });
    expect(client.getQueryData(taskKeys.detail("review"))).toEqual(saved);
  });
});
