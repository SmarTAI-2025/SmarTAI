import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as aiCompletionsApi from "@/api/aiCompletions";
import { aiCompletionKeys, taskKeys } from "./keys";

export function useAICompletionPreflight(taskId?: string) {
  return useQuery({
    queryKey: aiCompletionKeys.preflight(taskId ?? ""),
    queryFn: () => aiCompletionsApi.getAICompletionPreflight(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useStartAICompletion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: aiCompletionsApi.startAICompletion,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}

export function useAICompletionJob(taskId?: string, jobId?: string) {
  return useQuery({
    queryKey: aiCompletionKeys.detail(taskId ?? "", jobId ?? ""),
    queryFn: () => aiCompletionsApi.getAICompletionJob(taskId as string, jobId as string),
    enabled: Boolean(taskId && jobId),
    refetchInterval: (query) => query.state.data?.status === "running" ? 1_500 : false,
  });
}
