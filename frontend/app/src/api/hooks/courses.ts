import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as coursesApi from "@/api/courses";
import { courseKeys, taskKeys } from "./keys";

export function useCourses({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: courseKeys.list(),
    queryFn: coursesApi.listCourses,
    enabled,
  });
}

export function useCourseSearch(query: string) {
  const normalized = query.trim();
  return useQuery({
    queryKey: courseKeys.search(normalized),
    queryFn: () => coursesApi.searchCourses(normalized),
    enabled: normalized.length > 0,
  });
}

export function useCreateCourse() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: coursesApi.createCourse,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: courseKeys.all });
      queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}
