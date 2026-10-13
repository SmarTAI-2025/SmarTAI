import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as tagsApi from "@/api/tags";
import type { TagColor } from "@/types";
import { tagKeys, taskKeys } from "./keys";
import { useFrontierWorkspace } from "@/lib/frontierContext";

export function useTags() {
  const frontier = useFrontierWorkspace();
  return useQuery({
    queryKey: tagKeys.list(),
    queryFn: tagsApi.listTags,
    enabled: !frontier,
  });
}

export function useTagSearch(query: string) {
  const frontier = useFrontierWorkspace();
  const normalized = query.trim();
  return useQuery({
    queryKey: tagKeys.search(normalized),
    queryFn: () => tagsApi.searchTags(normalized),
    enabled: !frontier && normalized.length > 0,
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
