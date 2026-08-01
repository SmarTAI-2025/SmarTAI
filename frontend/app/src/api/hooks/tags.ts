import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as tagsApi from "@/api/tags";
import type { TagColor } from "@/types";
import { tagKeys, taskKeys } from "./keys";

export function useTags() {
  return useQuery({
    queryKey: tagKeys.list(),
    queryFn: tagsApi.listTags,
  });
}

export function useTagSearch(query: string) {
  const normalized = query.trim();
  return useQuery({
    queryKey: tagKeys.search(normalized),
    queryFn: () => tagsApi.searchTags(normalized),
    enabled: normalized.length > 0,
  });
}

export function useCreateTag() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: tagsApi.createTag,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: tagKeys.all }),
  });
}

export function useUpdateTag() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ tagId, patch }: { tagId: string; patch: { name?: string; color?: TagColor } }) =>
      tagsApi.updateTag(tagId, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagKeys.all });
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

export function useDeleteTag() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: tagsApi.deleteTag,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: tagKeys.all });
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}
