import type { MessageKey } from "@/i18n/messages";
import type { TaskStatus } from "@/types";

// Dashboard and history must present the same server-derived stage.
export const TASK_STAGE_KEYS: Record<TaskStatus, MessageKey> = {
  problems_paused: "taskStageProblemsPaused",
  submissions_paused: "taskStageSubmissionsPaused",
  grading_paused: "taskStageGradingPaused",
  draft: "dashboardStageDraft",
  extracting_problems: "dashboardStageExtracting",
  problems_ready: "dashboardStageProblemsReady",
  parsing_submissions: "dashboardStageParsing",
  submissions_ready: "dashboardStageSubmissionsReady",
  grading_setup: "dashboardStageGradingSetup",
  grading: "dashboardStageGrading",
  graded: "dashboardStageGraded",
  finalized: "dashboardStageFinalized",
  error: "dashboardStageError",
};

export const TASK_ACTION_KEYS: Record<TaskStatus, MessageKey> = {
  problems_paused: "taskActionContinueRecognition",
  submissions_paused: "taskActionContinueRecognition",
  grading_paused: "taskActionContinueGrading",
  draft: "dashboardActionContinue",
  extracting_problems: "dashboardActionViewProgress",
  problems_ready: "dashboardActionReviewProblems",
  parsing_submissions: "dashboardActionViewProgress",
  submissions_ready: "dashboardActionReviewSubmissions",
  grading_setup: "dashboardStageGradingSetup",
  grading: "dashboardActionViewProgress",
  graded: "dashboardActionViewResults",
  finalized: "dashboardActionOpenResults",
  error: "dashboardActionResolveError",
};

export function taskStatusTone(status: TaskStatus): string {
  switch (status) {
    case "extracting_problems":
    case "parsing_submissions":
    case "grading":
      return "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-200";
    case "draft":
    case "problems_ready":
    case "submissions_ready":
    case "grading_setup":
    case "problems_paused":
    case "submissions_paused":
    case "grading_paused":
      return "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-200";
    case "graded":
    case "finalized":
      return "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-200";
    case "error":
      return "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200";
  }
}
