import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as tasksApi from "@/api/tasks";
import type { UploadOptions } from "@/api/client";
import { taskKeys } from "./keys";

export function useTasks() {
  return useQuery({
    queryKey: taskKeys.list(),
    queryFn: tasksApi.listTasks,
  });
}

export function useTask(taskId?: string) {
  return useQuery({
    queryKey: taskKeys.detail(taskId ?? ""),
    queryFn: () => tasksApi.getTask(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useTaskState(taskId?: string, options: { refetchInterval?: number | false } = {}) {
  return useQuery({
    queryKey: taskKeys.state(taskId ?? ""),
    queryFn: () => tasksApi.getTaskState(taskId as string),
    enabled: Boolean(taskId),
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
    mutationFn: ({ taskId, name }: { taskId: string; name?: string | null }) =>
      tasksApi.updateTask(taskId, { name }),
    onSuccess: (task) => {
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
      queryClient.setQueryData(taskKeys.detail(task.task_id), task);
    },
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
  return useTaskUploadMutation((taskId, file, options) => tasksApi.parseSubmissions(taskId, file, options));
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
    mutationFn: ({
      taskId,
      qId,
      stem,
      criterion,
    }: {
      taskId: string;
      qId: string;
      stem?: string;
      criterion?: string;
    }) => tasksApi.updateProblem(taskId, qId, { stem, criterion }),
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
      content,
      flag,
    }: {
      taskId: string;
      studentId: string;
      qId: string;
      content?: string;
      flag?: string[];
    }) => tasksApi.updateStudentAnswer(taskId, studentId, qId, { content, flag }),
    onSuccess: (_data, variables) => {
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
      queryClient.invalidateQueries({ queryKey: taskKeys.result(variables.taskId) });
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
}
