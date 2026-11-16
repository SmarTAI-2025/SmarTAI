import { useTaskState } from "@/api/hooks/tasks";

const ACTIVE_STATUSES = new Set(["extracting_problems", "parsing_submissions", "grading"]);

export function useTaskProgress(taskId?: string, options: { enabled?: boolean } = {}) {
  const query = useTaskState(taskId, {
    enabled: options.enabled,
    refetchInterval: options.enabled === false ? false : 1_500,
  });

  const isActive = Boolean(query.data?.status && ACTIVE_STATUSES.has(query.data.status));
  const progress = query.data?.progress ?? null;
  // All three views consume the server projection, including unknown progress.
  // Do not derive a second percentage or round unfinished work to 100%.
  const value = query.data?.progress_percent;
  const percent = typeof value === "number" && Number.isFinite(value) ? value : null;

  return {
    ...query,
    isActive,
    progress,
    percent,
    latestMessage: progress?.messages.at(-1) ?? null,
  };
}
