import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as tasksApi from "@/api/tasks";
import type { UploadOptions } from "@/api/client";
import type {
  Task,
  TaskHistoryQuery,
  TaskFinalizationResponse,
  TaskMetadataPatch,
  TaskResultResponse,
  TaskStateSnapshot,
  TaskStatus,
} from "@/types";
import { tagKeys, taskKeys } from "./keys";

const ACTIVE_TASK_STATUSES = new Set<TaskStatus>([
  "extracting_problems",
  "parsing_submissions",
  "grading",
  "generating_analysis",
]);

export function useTasks() {
  return useQuery({
    queryKey: taskKeys.list(),
    queryFn: tasksApi.listTasks,
    refetchInterval: (query) => {
      const tasks = query.state.data;
      if (!tasks) {
        return false;
      }
      return Object.values(tasks).some((task) => ACTIVE_TASK_STATUSES.has(task.status))
        ? 3_000
        : false;
    },
  });
}

export function useTaskHistory(query: TaskHistoryQuery) {
  const queryKey = historyQueryKey(query);
  return useQuery({
    queryKey: taskKeys.history(queryKey),
    queryFn: () => tasksApi.listTaskHistory(query),
    placeholderData: (previous) => previous,
    refetchInterval: (historyQuery) => {
      const data = historyQuery.state.data;
      return data?.items?.some((task) => ACTIVE_TASK_STATUSES.has(task.status)) ? 3_000 : false;
    },
  });
}

export function useTask(taskId?: string, options: { pollAICompletion?: boolean } = {}) {
  return useQuery({
    queryKey: taskKeys.detail(taskId ?? ""),
    queryFn: () => tasksApi.getTask(taskId as string),
    enabled: Boolean(taskId),
    refetchInterval: options.pollAICompletion
      ? (query) => query.state.data?.ai_completion_job_id ? 1_500 : false
      : false,
  });
}

export function useTaskState(taskId?: string, options: { refetchInterval?: number | false; enabled?: boolean } = {}) {
  return useQuery({
    queryKey: taskKeys.state(taskId ?? ""),
    queryFn: () => tasksApi.getTaskState(taskId as string),
    enabled: Boolean(taskId) && (options.enabled ?? true),
    refetchInterval: options.refetchInterval,
  });
}

export function useTaskResult(taskId?: string) {
  return useQuery({
    queryKey: taskKeys.result(taskId ?? ""),
    queryFn: () => tasksApi.getTaskResult(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useTaskFinalization(taskId?: string) {
  return useQuery({
    queryKey: taskKeys.finalization(taskId ?? ""),
    queryFn: () => tasksApi.getTaskFinalization(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useTaskResultArtifacts(taskId?: string) {
  return useQuery({
    queryKey: taskKeys.artifacts(taskId ?? ""),
    queryFn: () => tasksApi.getTaskResultArtifacts(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useGenerateTaskResultArtifacts() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, expectedWorkflowRevision }: { taskId: string; expectedWorkflowRevision: number }) =>
      tasksApi.generateTaskResultArtifacts(taskId, expectedWorkflowRevision),
    onSuccess: (data, variables) => {
      queryClient.setQueryData(taskKeys.artifacts(variables.taskId), data.artifacts);
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useConfirmTaskFinalization() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      taskId,
      expectedWorkflowRevision,
    }: {
      taskId: string;
      expectedWorkflowRevision: number;
    }) => tasksApi.confirmTaskFinalization(taskId, expectedWorkflowRevision),
    onSuccess: (data, variables) => {
      queryClient.setQueryData(taskKeys.finalization(variables.taskId), data);
      queryClient.setQueryData<Task>(taskKeys.detail(variables.taskId), (current) => (
        current
          ? { ...current, status: data.task_status, workflow_revision: data.workflow_revision }
          : current
      ));
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useTeacherComments(taskId?: string) {
  return useQuery({
    queryKey: taskKeys.comments(taskId ?? ""),
    queryFn: () => tasksApi.listTeacherComments(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useCreateTask() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: tasksApi.createTask,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export function useUpdateTask() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, patch, name }: { taskId: string; patch?: TaskMetadataPatch; name?: string | null }) =>
      tasksApi.updateTask(taskId, patch ?? { name }),
    onSuccess: (task) => {
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
      queryClient.invalidateQueries({ queryKey: tagKeys.all });
      // The update endpoint returns TaskLite while the detail cache stores a
      // full Task. Let the invalidation refetch instead of replacing detail
      // data and accidentally dropping problem_data/student_data.
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(task.task_id) });
    },
  });
}

export function useInterpretTaskHistoryQuery() {
  return useMutation({
    mutationFn: tasksApi.interpretTaskHistoryQuery,
  });
}

export function useDeleteTask() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: tasksApi.deleteTask,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export function useExtractProblems() {
  return useTaskUploadMutation((taskId, file, options) => tasksApi.extractProblems(taskId, file, options));
}

export function useParseSubmissions() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: tasksApi.parseSubmissions,
    onSuccess: (data, variables) => {
      if (data.status === "started" || data.status === "already_running") {
        const activeTaskPatch = {
          status: "parsing_submissions" as const,
          parse_job_id: data.job_id ?? null,
        };
        queryClient.setQueryData<Task>(taskKeys.detail(variables.taskId), (current) => (
          current ? { ...current, ...activeTaskPatch } : current
        ));
        queryClient.setQueryData<TaskStateSnapshot>(taskKeys.state(variables.taskId), (current) => (
          current ? { ...current, ...activeTaskPatch } : current
        ));
      }
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useUploadReference() {
  return useTaskUploadMutation((taskId, file, options) => tasksApi.uploadReference(taskId, file, options));
}

export function useUploadTestCases() {
  return useTaskUploadMutation((taskId, file, options) => tasksApi.uploadTestCases(taskId, file, options));
}

export function useStartGrading() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, multiSampleN }: { taskId: string; multiSampleN?: number | null }) =>
      tasksApi.startGrading(taskId, { multiSampleN }),
    onSuccess: (_data, variables) => {
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useUpdateProblem() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, qId, ...patch }: {
      taskId: string;
      qId: string;
      stem?: string;
      criterion?: string;
      review_status?: "needs_review" | "edited" | "confirmed";
      reference_answer?: string | null;
      solution_code?: string | null;
      test_cases?: import("@/types").TestCase[] | null;
    }) => tasksApi.updateProblem(taskId, qId, patch),
    onSuccess: (_data, variables) => {
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useUpdateStudentAnswer() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      taskId,
      studentId,
      qId,
      expectedWorkflowRevision,
      content,
      flag,
      reviewStatus,
    }: {
      taskId: string;
      studentId: string;
      qId: string;
      expectedWorkflowRevision?: number;
      content?: string;
      flag?: string[];
      reviewStatus?: "pending" | "confirmed";
    }) => tasksApi.updateStudentAnswer(taskId, studentId, qId, {
      expected_workflow_revision: expectedWorkflowRevision,
      content,
      flag,
      review_status: reviewStatus,
    }),
    onSuccess: (data, variables) => {
      queryClient.setQueryData<Task>(taskKeys.detail(variables.taskId), (current) => {
        const student = current?.student_data[variables.studentId];
        if (!current || !student) return current;
        const answers = [...student.stu_ans];
        const answerIndex = answers.findIndex((answer) => answer.q_id === variables.qId);
        if (answerIndex >= 0) answers[answerIndex] = data.answer;
        else answers.push(data.answer);
        return {
          ...current,
          workflow_revision: data.workflow_revision,
          student_data: {
            ...current.student_data,
            [variables.studentId]: { ...student, stu_ans: answers },
          },
        };
      });
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useUpdateStudentIdentity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      taskId,
      currentStudentId,
      expectedWorkflowRevision,
      studentId,
      studentName,
    }: {
      taskId: string;
      currentStudentId: string;
      expectedWorkflowRevision: number;
      studentId: string;
      studentName: string;
    }) => tasksApi.updateStudentIdentity(taskId, currentStudentId, {
      expected_workflow_revision: expectedWorkflowRevision,
      student_id: studentId,
      student_name: studentName,
    }),
    onSuccess: (data, variables) => {
      queryClient.setQueryData<Task>(taskKeys.detail(variables.taskId), (current) => {
        if (!current) return current;
        const studentData = { ...current.student_data };
        delete studentData[data.previous_student_id];
        studentData[data.student.stu_id] = data.student;
        return {
          ...current,
          student_data: studentData,
          workflow_revision: data.workflow_revision,
        };
      });
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

export function useSetTeacherComment() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      taskId,
      studentId,
      qId,
      comment,
    }: {
      taskId: string;
      studentId: string;
      qId: string;
      comment: string;
    }) => tasksApi.setTeacherComment(taskId, studentId, qId, comment),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: taskKeys.comments(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.finalization(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.result(variables.taskId) });
    },
  });
}

export function useUpdateCorrectionReview() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      taskId,
      studentId,
      qId,
      ...input
    }: {
      taskId: string;
      studentId: string;
      qId: string;
      expected_workflow_revision: number;
      teacher_score: number;
      teacher_comment: string;
      confirm: boolean;
    }) => tasksApi.updateCorrectionReview(taskId, studentId, qId, input),
    onSuccess: (data, variables) => {
      queryClient.setQueryData<Task>(taskKeys.detail(variables.taskId), (current) =>
        current ? { ...current, status: "graded", workflow_revision: data.workflow_revision } : current,
      );
      queryClient.setQueryData<TaskResultResponse>(taskKeys.result(variables.taskId), (current) => {
        if (!current?.results) return current;
        return {
          ...current,
          results: current.results.map((student) => student.student_id === variables.studentId
            ? {
                ...student,
                corrections: student.corrections.map((correction) => correction.q_id === variables.qId ? data.correction : correction),
              }
            : student),
        };
      });
      if (data.correction.review_status === "confirmed") {
        queryClient.setQueryData<TaskFinalizationResponse>(taskKeys.finalization(variables.taskId), (current) => {
          if (!current) return current;
          const remainingReviews = current.remaining_reviews.filter((review) => (
            review.student_id !== variables.studentId || review.q_id !== variables.qId
          ));
          const newlyConfirmedCount = current.remaining_reviews.length - remainingReviews.length;
          return {
            ...current,
            task_status: "graded",
            workflow_revision: data.workflow_revision,
            ready_for_confirmation: remainingReviews.length === 0,
            confirmed_required_count: Math.min(
              current.required_review_count,
              current.confirmed_required_count + newlyConfirmedCount,
            ),
            remaining_review_count: remainingReviews.length,
            remaining_reviews: remainingReviews,
            final_result_dirty: current.final_result_version > 0 || current.final_result_dirty,
            analysis_status: current.analysis_status === "ready" ? "stale" : current.analysis_status,
          };
        });
      }
      queryClient.invalidateQueries({ queryKey: taskKeys.list() });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.comments(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.finalization(variables.taskId) });
    },
  });
}

function useTaskUploadMutation(
  uploadFn: (taskId: string, file: File, options?: UploadOptions) => ReturnType<typeof tasksApi.extractProblems>,
) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, file, onProgress }: { taskId: string; file: File; onProgress?: UploadOptions["onProgress"] }) =>
      uploadFn(taskId, file, { onProgress }),
    onSuccess: (_data, variables) => {
      invalidateTask(queryClient, variables.taskId);
    },
  });
}

function invalidateTask(queryClient: ReturnType<typeof useQueryClient>, taskId: string) {
  queryClient.invalidateQueries({ queryKey: taskKeys.all });
  queryClient.invalidateQueries({ queryKey: taskKeys.detail(taskId) });
  queryClient.invalidateQueries({ queryKey: taskKeys.state(taskId) });
  queryClient.invalidateQueries({ queryKey: taskKeys.result(taskId) });
  queryClient.invalidateQueries({ queryKey: taskKeys.finalization(taskId) });
  queryClient.invalidateQueries({ queryKey: taskKeys.artifacts(taskId) });
}

function historyQueryKey(query: TaskHistoryQuery): string {
  return JSON.stringify({
    page: query.page,
    page_size: query.page_size,
    q: query.q ?? "",
    semester_id: query.semester_id ?? "",
    course_id: query.course_id ?? "",
    tag_ids: [...(query.tag_ids ?? [])].sort(),
    statuses: [...(query.statuses ?? [])].sort(),
    unfinished: Boolean(query.unfinished),
    needs_attention: Boolean(query.needs_attention),
    sort: query.sort,
  });
}
