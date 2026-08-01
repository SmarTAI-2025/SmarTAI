import type { MessageKey } from "@/i18n/messages";
import type { TagColor, TaskStatus } from "@/types";

export { formatSemesterLabel } from "@/lib/semesters";

export const HISTORY_STAGE_KEYS: Record<TaskStatus, MessageKey> = {
  draft: "dashboardStageDraft",
  extracting_problems: "dashboardStageExtracting",
  problems_ready: "dashboardStageProblemsReady",
  parsing_submissions: "dashboardStageParsing",
  submissions_ready: "dashboardStageSubmissionsReady",
  grading: "dashboardStageGrading",
  graded: "dashboardStageGraded",
  review_confirmed: "dashboardStageReviewConfirmed",
  generating_analysis: "dashboardStageGeneratingAnalysis",
  finalized: "dashboardStageFinalized",
  error: "dashboardStageError",
};

export const HISTORY_ACTION_KEYS: Record<TaskStatus, MessageKey> = {
  draft: "dashboardActionContinue",
  extracting_problems: "dashboardActionViewProgress",
  problems_ready: "dashboardActionReviewProblems",
  parsing_submissions: "dashboardActionViewProgress",
  submissions_ready: "dashboardActionReviewSubmissions",
  grading: "dashboardActionViewProgress",
  graded: "dashboardActionViewResults",
  review_confirmed: "dashboardActionOpenResults",
  generating_analysis: "dashboardActionOpenResults",
  finalized: "dashboardActionOpenResults",
  error: "dashboardActionResolveError",
};

export const HISTORY_STATUS_OPTIONS = (Object.keys(HISTORY_STAGE_KEYS) as TaskStatus[]).map((status) => ({
  status,
  labelKey: HISTORY_STAGE_KEYS[status],
}));

export const TAG_COLORS: TagColor[] = ["slate", "blue", "teal", "green", "amber", "rose", "violet"];

export const TAG_COLOR_LABEL_KEYS: Record<TagColor, MessageKey> = {
  slate: "historyTagColorSlate",
  blue: "historyTagColorBlue",
  teal: "historyTagColorTeal",
  green: "historyTagColorGreen",
  amber: "historyTagColorAmber",
  rose: "historyTagColorRose",
  violet: "historyTagColorViolet",
};

export const TAG_TONE_CLASSES: Record<TagColor, string> = {
  slate: "border-slate-200 bg-slate-100 text-slate-600 dark:border-slate-600 dark:bg-slate-700/60 dark:text-slate-200",
  blue: "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950/60 dark:text-blue-200",
  teal: "border-teal-200 bg-teal-50 text-teal-700 dark:border-teal-800 dark:bg-teal-950/60 dark:text-teal-200",
  green: "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-950/60 dark:text-green-200",
  amber: "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-200",
  rose: "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800 dark:bg-rose-950/60 dark:text-rose-200",
  violet: "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-800 dark:bg-violet-950/60 dark:text-violet-200",
};

export function historyStatusTone(status: TaskStatus): string {
  switch (status) {
    case "extracting_problems":
    case "parsing_submissions":
      return "bg-blue-100 text-blue-700 dark:bg-blue-950/60 dark:text-blue-200";
    case "problems_ready":
    case "submissions_ready":
      return "bg-teal-100 text-teal-700 dark:bg-teal-950/60 dark:text-teal-200";
    case "grading":
    case "graded":
      return "bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-200";
    case "generating_analysis":
      return "bg-blue-100 text-blue-700 dark:bg-blue-950/60 dark:text-blue-200";
    case "review_confirmed":
    case "finalized":
      return "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-200";
    case "error":
      return "bg-rose-100 text-rose-700 dark:bg-rose-950/60 dark:text-rose-200";
    case "draft":
    default:
      return "bg-slate-100 text-slate-600 dark:bg-slate-700/60 dark:text-slate-200";
  }
}

export function formatHistoryTime(timestamp: number | undefined, locale: string): string {
  if (!Number.isFinite(timestamp) || !timestamp || timestamp <= 0) return "—";
  const seconds = timestamp - Date.now() / 1_000;
  const absolute = Math.abs(seconds);
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (absolute < 60) return relative.format(Math.round(seconds), "second");
  if (absolute < 3_600) return relative.format(Math.round(seconds / 60), "minute");
  if (absolute < 86_400) return relative.format(Math.round(seconds / 3_600), "hour");
  if (absolute < 604_800) return relative.format(Math.round(seconds / 86_400), "day");
  return new Intl.DateTimeFormat(locale, { year: "numeric", month: "2-digit", day: "2-digit" })
    .format(new Date(timestamp * 1_000));
}
