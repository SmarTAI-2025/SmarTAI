import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as analyticsApi from "@/api/analytics";
import type { AnalyticsMode } from "@/types";
import { analyticsKeys } from "./keys";

export function useAnalyticsQuery() {
  return useMutation({
    mutationFn: ({ taskId, question, mode }: { taskId: string; question: string; mode: AnalyticsMode }) =>
      analyticsApi.runAnalyticsQuery(taskId, question, mode),
  });
}

export function usePerQuestionBreakdown(taskId?: string, qId?: string) {
  return useQuery({
    queryKey: analyticsKeys.perQuestion(taskId ?? "", qId ?? ""),
    queryFn: () => analyticsApi.getPerQuestionBreakdown(taskId as string, qId as string),
    enabled: Boolean(taskId && qId),
  });
}

export function useResetPerQuestionCache() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, qId }: { taskId: string; qId: string }) =>
      analyticsApi.resetPerQuestionCache(taskId, qId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: analyticsKeys.perQuestion(variables.taskId, variables.qId),
      });
    },
  });
}
