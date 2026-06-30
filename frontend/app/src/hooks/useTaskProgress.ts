import { useMemo } from "react";
import { useTaskState } from "@/api/hooks/tasks";

const ACTIVE_STATUSES = new Set(["extracting_problems", "parsing_submissions", "grading"]);

export function useTaskProgress(taskId?: string) {
  const query = useTaskState(taskId, { refetchInterval: 1_500 });

  const isActive = Boolean(query.data?.status && ACTIVE_STATUSES.has(query.data.status));
  const progress = query.data?.progress ?? null;
  const percent = useMemo(() => {
    if (!progress) {
      return 0;
    }

    if (progress.total_students > 0 && progress.total_questions > 0) {
      const totalUnits = progress.total_students * progress.total_questions;
      return Math.min(100, Math.round((progress.completed_units / totalUnits) * 100));
    }

    if (progress.total_students > 0) {
      return Math.min(100, Math.round((progress.completed_units / progress.total_students) * 100));
    }

    return progress.phase === "done" ? 100 : 0;
  }, [progress]);

  return {
    ...query,
    isActive,
    progress,
    percent,
    latestMessage: progress?.messages.at(-1) ?? null,
  };
}
