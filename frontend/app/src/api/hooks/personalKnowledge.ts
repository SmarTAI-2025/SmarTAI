import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as personalKnowledgeApi from "@/api/personalKnowledge";
import type { UploadOptions } from "@/api/client";
import { personalKnowledgeKeys } from "./keys";

export function usePersonalKnowledge() {
  return useQuery({ queryKey: personalKnowledgeKeys.list(), queryFn: personalKnowledgeApi.listPersonalKnowledge });
}

export function useKnowledgeStorageUsage() {
  return useQuery({
    queryKey: personalKnowledgeKeys.usage(),
    queryFn: personalKnowledgeApi.getKnowledgeStorageUsage,
    refetchInterval: (query) => {
      const usage = query.state.data;
      return usage && (usage.cleanup_pending_count > 0 || usage.retrying_cleanup_count > 0)
        ? 3_000
        : false;
    },
  });
}

function invalidatePersonalKnowledge(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: personalKnowledgeKeys.list() });
}

function invalidateStorageUsage(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: personalKnowledgeKeys.usage() });
}

export function useUploadPersonalKnowledge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, onProgress }: { file: File; onProgress?: UploadOptions["onProgress"] }) =>
      personalKnowledgeApi.uploadPersonalKnowledge(file, { onProgress }),
    onSuccess: () => invalidatePersonalKnowledge(queryClient),
    onSettled: () => invalidateStorageUsage(queryClient),
  });
}

export function useDeletePersonalKnowledge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) => personalKnowledgeApi.deletePersonalKnowledge(documentId),
    onSuccess: () => invalidatePersonalKnowledge(queryClient),
    onSettled: () => invalidateStorageUsage(queryClient),
  });
}
