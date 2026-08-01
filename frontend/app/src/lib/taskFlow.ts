import type { Task, TaskLite } from "@/types";

const PROCESSING_STATUSES = new Set(["extracting_problems", "parsing_submissions", "grading", "generating_analysis"]);
const COMPLETED_GRADING_STATUSES = new Set(["graded", "review_confirmed", "generating_analysis", "finalized"]);
const REGRADABLE_STATUSES = new Set(["graded", "review_confirmed", "finalized"]);

export function isTaskProcessing(status?: string | null): boolean {
  return Boolean(status && PROCESSING_STATUSES.has(status));
}

export function hasTaskCompletedGrading(status?: string | null): boolean {
  return Boolean(status && COMPLETED_GRADING_STATUSES.has(status));
}

export function canTaskBeRegraded(status?: string | null): boolean {
  return Boolean(status && REGRADABLE_STATUSES.has(status));
}

type TaskDestinationInput = Pick<TaskLite, "task_id" | "status" | "grading_setup_configured"> & Partial<Pick<
  TaskLite,
  | "last_failed_job_id"
  | "extract_job_id"
  | "parse_job_id"
  | "grading_job_id"
  | "problem_file_name"
  | "submission_file_name"
  | "problem_count"
  | "student_count"
>>;

type TaskReachabilityInput = Partial<TaskDestinationInput> & Partial<Pick<Task, "problem_data">>;

export function getTaskReachableStep(task?: TaskReachabilityInput | null): number {
  if (!task) return 0;
  switch (task.status) {
    case "draft":
      return (task.problem_count ?? 0) > 0 ? 2 : 1;
    case "extracting_problems":
      return 1;
    case "problems_ready":
      return task.grading_setup_configured || allProblemsConfirmed(task.problem_data) ? 3 : 2;
    case "parsing_submissions":
      return 3;
    case "submissions_ready":
      // Once recognition is complete, both the review surface (step 5) and
      // grading setup (step 6) are valid.  Teachers can move forward after
      // review without an otherwise-disabled stepper target.
      return 5;
    case "grading":
      return 5;
    case "graded":
      return 6;
    case "review_confirmed":
    case "generating_analysis":
    case "finalized":
      return 7;
    case "error":
      if (task.grading_job_id) return 5;
      if (task.parse_job_id || task.submission_file_name || (task.student_count ?? 0) > 0) return 4;
      if ((task.problem_count ?? 0) > 0) return allProblemsConfirmed(task.problem_data) ? 3 : 2;
      if (task.extract_job_id || task.problem_file_name) return 1;
      return 1;
    default:
      return 0;
  }
}

export function hasTaskReachedStep(task: TaskReachabilityInput | null | undefined, step: number): boolean {
  return getTaskReachableStep(task) >= step;
}

function allProblemsConfirmed(problemData?: Task["problem_data"]) {
  const problems = Object.values(problemData ?? {});
  return problems.length > 0 && problems.every((problem) => problem.review_status === "confirmed");
}

export function getTaskDestination(task: TaskDestinationInput): string {
  const taskRoot = `/tasks/${task.task_id}`;

  switch (task.status) {
    case "extracting_problems":
      return `${taskRoot}/problems/progress`;
    case "problems_ready":
      return task.grading_setup_configured
        ? `${taskRoot}/submissions/upload`
        : `${taskRoot}/questions`;
    case "parsing_submissions":
      return `${taskRoot}/submissions/progress`;
    case "submissions_ready":
      return `${taskRoot}/submissions`;
    case "grading":
      return `${taskRoot}/grading/progress`;
    case "graded":
      return `${taskRoot}/review`;
    case "review_confirmed":
    case "generating_analysis":
    case "finalized":
      return `${taskRoot}/results`;
    case "draft":
      return `${taskRoot}/upload/problems`;
    case "error": {
      const failedJobId = task.last_failed_job_id;

      if (failedJobId && failedJobId === task.grading_job_id) {
        return `${taskRoot}/grading/progress`;
      }
      if (failedJobId && failedJobId === task.parse_job_id) {
        return `${taskRoot}/submissions/progress`;
      }
      if (failedJobId && failedJobId === task.extract_job_id) {
        return `${taskRoot}/problems/progress`;
      }

      // Older task snapshots may not expose last_failed_job_id. Recover to
      // the furthest stage supported by persisted job/file/count evidence.
      if (task.grading_job_id) {
        return `${taskRoot}/grading/progress`;
      }
      if (task.parse_job_id) {
        return `${taskRoot}/submissions/progress`;
      }
      if (task.submission_file_name || (task.student_count ?? 0) > 0) {
        return `${taskRoot}/submissions/upload`;
      }
      if ((task.problem_count ?? 0) > 0) {
        return `${taskRoot}/questions`;
      }
      if (task.extract_job_id || task.problem_file_name) {
        return `${taskRoot}/problems/progress`;
      }
      return `${taskRoot}/upload/problems`;
    }
    default:
      return `${taskRoot}/upload/problems`;
  }
}

export function getTaskGradingSetupHref(taskId: string, returnTo?: string): string {
  const base = `/tasks/${encodeURIComponent(taskId)}/grading-setup`;
  return returnTo ? `${base}?returnTo=${encodeURIComponent(returnTo)}` : base;
}

export function getSafeTaskReturnTo(taskId: string, value?: string | null): string | null {
  if (!value) return null;
  const taskPrefix = `/tasks/${encodeURIComponent(taskId)}/`;
  if (!value.startsWith(taskPrefix) || value.startsWith("//")) return null;
  return value;
}

export function formatTaskTime(
  timestamp: number | undefined,
  includeYear = false,
  locale = "zh-CN",
): string {
  if (!Number.isFinite(timestamp) || !timestamp || timestamp <= 0) {
    return "—";
  }

  return new Intl.DateTimeFormat(locale, {
    ...(includeYear ? { year: "numeric" as const } : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}
