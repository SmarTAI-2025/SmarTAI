import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as materialImportsApi from "@/api/materialImports";
import { materialImportKeys, taskKeys } from "./keys";

export function usePreflightMaterialImport() {
  return useMutation({ mutationFn: materialImportsApi.preflightMaterialImport });
}

export function useStartMaterialImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, sourceToken }: { taskId: string; sourceToken: string }) =>
      materialImportsApi.startMaterialImport(taskId, sourceToken),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}

export function useMaterialImport(taskId?: string, jobId?: string) {
  return useQuery({
    queryKey: materialImportKeys.detail(taskId ?? "", jobId ?? ""),
    queryFn: () => materialImportsApi.getMaterialImport(taskId as string, jobId as string),
    enabled: Boolean(taskId && jobId),
    refetchInterval: (query) => query.state.data?.status === "running" ? 1_500 : false,
  });
}

export function useApplyMaterialImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: materialImportsApi.applyMaterialImport,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: materialImportKeys.detail(variables.taskId, variables.jobId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}
