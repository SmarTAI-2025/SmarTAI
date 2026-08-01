import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as personalKnowledgeApi from "@/api/personalKnowledge";
import type { UploadOptions } from "@/api/client";
import { personalKnowledgeKeys } from "./keys";

export function usePersonalKnowledge() {
  return useQuery({ queryKey: personalKnowledgeKeys.list(), queryFn: personalKnowledgeApi.listPersonalKnowledge });
}

export function useUploadPersonalKnowledge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, onProgress }: { file: File; onProgress?: UploadOptions["onProgress"] }) =>
      personalKnowledgeApi.uploadPersonalKnowledge(file, { onProgress }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: personalKnowledgeKeys.list() }),
  });
}

export function useDeletePersonalKnowledge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) => personalKnowledgeApi.deletePersonalKnowledge(documentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: personalKnowledgeKeys.list() }),
  });
}
