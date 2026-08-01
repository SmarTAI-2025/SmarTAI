import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as courseMaterialsApi from "@/api/courseMaterials";
import type { CourseMaterialListParams } from "@/types";
import { courseMaterialKeys, problemSourceKeys } from "./keys";

function paramsKey(params: CourseMaterialListParams) {
  return JSON.stringify({
    q: params.q?.trim() ?? "",
    course_id: params.course_id ?? "",
    group_id: params.group_id ?? "",
    category: params.category ?? "",
    page: params.page ?? 1,
    page_size: params.page_size ?? 30,
  });
}

function invalidateLibrary(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: courseMaterialKeys.all });
  queryClient.invalidateQueries({ queryKey: problemSourceKeys.all });
}

export function useCourseMaterials(params: CourseMaterialListParams) {
  return useQuery({
    queryKey: courseMaterialKeys.list(paramsKey(params)),
    queryFn: () => courseMaterialsApi.listCourseMaterials(params),
  });
}

export function useCourseMaterialGroups(q = "") {
  const normalized = q.trim();
  return useQuery({
    queryKey: courseMaterialKeys.groups(normalized),
    queryFn: () => courseMaterialsApi.listCourseMaterialGroups(normalized),
  });
}

export function useUploadCourseMaterial() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: courseMaterialsApi.uploadCourseMaterial,
    onSuccess: () => invalidateLibrary(queryClient),
  });
}

export function useUpdateCourseMaterial() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: courseMaterialsApi.updateCourseMaterial,
    onSuccess: () => invalidateLibrary(queryClient),
  });
}

export function useDeleteCourseMaterial() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ materialId, confirmReferenced }: { materialId: string; confirmReferenced: boolean }) =>
      courseMaterialsApi.deleteCourseMaterial(materialId, confirmReferenced),
    onSuccess: () => invalidateLibrary(queryClient),
  });
}

export function useCreateCourseMaterialGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: courseMaterialsApi.createCourseMaterialGroup,
    onSuccess: () => invalidateLibrary(queryClient),
  });
}

export function useUpdateCourseMaterialGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: courseMaterialsApi.updateCourseMaterialGroup,
    onSuccess: () => invalidateLibrary(queryClient),
  });
}

export function useDeleteCourseMaterialGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: courseMaterialsApi.deleteCourseMaterialGroup,
    onSuccess: () => invalidateLibrary(queryClient),
  });
}
