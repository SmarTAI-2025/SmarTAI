import { useMutation } from "@tanstack/react-query";
import * as analyticsApi from "@/api/analytics";
import type { AnalyticsMode } from "@/types";

export function useAnalyticsQuery() {
  return useMutation({
    mutationFn: ({ taskId, question, mode, history }: { taskId: string; question: string; mode: AnalyticsMode; history?: string[] }) =>
      analyticsApi.runAnalyticsQuery(taskId, question, mode, history),
  });
}
