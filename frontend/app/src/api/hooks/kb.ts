import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as kbApi from "@/api/kb";
import type { AddKBDocInput } from "@/api/kb";
import { gradingSetupKeys, kbKeys, taskKeys } from "./keys";

export function useKBDocs(taskId?: string) {
  return useQuery({
    queryKey: kbKeys.list(taskId ?? ""),
    queryFn: () => kbApi.listKBDocs(taskId as string),
    enabled: Boolean(taskId),
  });
}

export function useUploadKBDoc() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, ...input }: { taskId: string } & AddKBDocInput) =>
      kbApi.addKBDoc(taskId, input),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: kbKeys.list(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: gradingSetupKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}

export function useDeleteKBDoc() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, docId, expectedWorkflowRevision }: { taskId: string; docId: string; expectedWorkflowRevision?: number }) =>
      kbApi.deleteKBDoc(taskId, docId, expectedWorkflowRevision),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: kbKeys.list(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: gradingSetupKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}
