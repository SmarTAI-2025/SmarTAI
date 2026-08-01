import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as problemSourcesApi from "@/api/problemSources";
import type { ProblemSourceScope, Task, TaskStateSnapshot } from "@/types";
import { problemSourceKeys, taskKeys } from "./keys";

export function useProblemSourceLibrary(
  taskId: string | undefined,
  scope: ProblemSourceScope,
  query: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: problemSourceKeys.library(taskId ?? "", scope, query),
    queryFn: () => problemSourcesApi.listProblemSourceLibrary(taskId as string, { scope, query }),
    enabled: Boolean(taskId) && enabled,
  });
}

export function useQuestionPreparationCapabilities(taskId?: string) {
  return useQuery({
    queryKey: problemSourceKeys.capabilities(taskId ?? ""),
    queryFn: () => problemSourcesApi.getQuestionPreparationCapabilities(taskId as string),
    enabled: Boolean(taskId),
    staleTime: 5 * 60 * 1000,
  });
}

export function useProblemSourcePreflight() {
  return useMutation({ mutationFn: problemSourcesApi.preflightProblemSource });
}

export function useStartProblemExtraction() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: problemSourcesApi.startProblemExtraction,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}

export function useStartQuestionPreparation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: problemSourcesApi.startQuestionPreparation,
    onSuccess: (data, variables) => {
      if (["started", "already_running"].includes(data.status)) {
        const activePatch = {
          status: "extracting_problems" as const,
          extract_job_id: data.job_id ?? null,
        };
        queryClient.setQueryData<Task>(taskKeys.detail(variables.taskId), (current) => (
          current ? { ...current, ...activePatch } : current
        ));
        queryClient.setQueryData<TaskStateSnapshot>(taskKeys.state(variables.taskId), (current) => (
          current
            ? {
                ...current,
                ...activePatch,
                active_job_id: data.job_id ?? current.active_job_id ?? null,
                active_operation: "question_preparation",
              }
            : current
        ));
      }
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}
