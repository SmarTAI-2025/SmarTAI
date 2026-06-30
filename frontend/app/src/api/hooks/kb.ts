import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as kbApi from "@/api/kb";
import type { UploadOptions } from "@/api/client";
import { kbKeys, taskKeys } from "./keys";

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
    mutationFn: ({ taskId, file, onProgress }: { taskId: string; file: File; onProgress?: UploadOptions["onProgress"] }) =>
      kbApi.uploadKBDoc(taskId, file, { onProgress }),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: kbKeys.list(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}

export function useDeleteKBDoc() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, docId }: { taskId: string; docId: string }) => kbApi.deleteKBDoc(taskId, docId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: kbKeys.list(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.detail(variables.taskId) });
      queryClient.invalidateQueries({ queryKey: taskKeys.state(variables.taskId) });
    },
  });
}
