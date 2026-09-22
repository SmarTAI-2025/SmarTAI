import { useMemo } from "react";
import { useTaskState } from "@/api/hooks/tasks";
import type { JobProgress, TaskStatus } from "@/types";

const ACTIVE_STATUSES = new Set(["extracting_problems", "parsing_submissions", "grading"]);

export function useTaskProgress(taskId?: string, options: { enabled?: boolean } = {}) {
  const query = useTaskState(taskId, {
    enabled: options.enabled,
    refetchInterval: options.enabled === false ? false : 1_500,
  });

  const isActive = Boolean(query.data?.status && ACTIVE_STATUSES.has(query.data.status));
  const progress = query.data?.progress ?? null;
  const percent = useMemo(
    () => calculateProgressPercent(progress, query.data?.status),
    [progress, query.data?.status],
  );

  return {
    ...query,
    isActive,
    progress,
    percent,
    latestMessage: progress?.messages.at(-1) ?? null,
  };
}

export function calculateProgressPercent(
  progress: JobProgress | null,
  status?: TaskStatus | string,
): number {
  if (!progress) {
    return 0;
  }

  if (
    typeof progress.total_steps === "number" &&
    progress.total_steps > 0 &&
    typeof progress.completed_steps === "number"
  ) {
    const metrics = progress.stage_metrics ?? {};
    const totalQuestions = metrics.solution_total_questions;
    const completedQuestions = metrics.solution_completed_questions;
    const hasQuestionGenerationProgress = progress.current_step === "generating_solutions"
      && typeof totalQuestions === "number"
      && Number.isFinite(totalQuestions)
      && totalQuestions > 0
      && typeof completedQuestions === "number"
      && Number.isFinite(completedQuestions);
    if (hasQuestionGenerationProgress) {
      const generationFraction = Math.max(
        0,
        Math.min(1, completedQuestions / totalQuestions),
      );
      return Math.min(
        100,
        Math.max(
          Math.round((progress.completed_steps / progress.total_steps) * 100),
          Math.floor(((progress.completed_steps + generationFraction) / progress.total_steps) * 100),
        ),
      );
    }
    return Math.min(
      100,
      Math.round((progress.completed_steps / progress.total_steps) * 100),
    );
  }

  if (progress.phase === "done") {
    return 100;
  }

  const totalUnits = getProgressTotalUnits(progress, status);
  if (totalUnits <= 0) {
    return 0;
  }
  return Math.min(100, Math.round((progress.completed_units / totalUnits) * 100));
}

export function getProgressTotalUnits(
  progress: JobProgress,
  status?: TaskStatus | string,
): number {
  if (status === "parsing_submissions" || progress.phase === "parsing") {
    return progress.total_students;
  }

  if (status === "grading" || progress.phase === "grading") {
    if (progress.total_students > 0 && progress.total_questions > 0) {
      return progress.total_students * progress.total_questions;
    }
  }

  return Math.max(progress.total_students, progress.total_questions);
}
