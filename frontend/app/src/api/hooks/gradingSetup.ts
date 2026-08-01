import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as gradingSetupApi from "@/api/gradingSetup";
import { gradingSetupKeys, taskKeys } from "./keys";

export function useGradingSetup(taskId?: string) {
  return useQuery({
    queryKey: gradingSetupKeys.detail(taskId ?? ""),
    queryFn: () => gradingSetupApi.getGradingSetup(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useSaveGradingSetup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: gradingSetupApi.saveGradingSetup,
    onSuccess: (response, variables) => {
      queryClient.setQueryData(gradingSetupKeys.detail(variables.taskId), response);
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}
