import type { Locale } from "@/i18n/messages";
import type { WorkflowTimeoutMinutes } from "@/types/health";

const workflowTimeoutKinds: Readonly<Partial<Record<string, keyof WorkflowTimeoutMinutes>>> = {
  question_preparation_time_limit_reached: "question_preparation",
  question_preparation_wall_time_limit_reached: "question_preparation",
  submission_time_limit_reached: "submission_recognition",
  submission_wall_time_limit_reached: "submission_recognition",
  grading_time_limit_reached: "grading",
  grading_wall_time_limit_reached: "grading",
  knowledge_wall_time_limit_reached: "knowledge_ingestion",
};

export function isWorkflowTimeout(errorCode: string | null | undefined): boolean {
  return Boolean(errorCode && Object.hasOwn(workflowTimeoutKinds, errorCode));
}

export function workflowTimeoutNote(
  errorCode: string | null | undefined, locale: Locale, limits: WorkflowTimeoutMinutes | undefined,
): string {
  const kind = errorCode && workflowTimeoutKinds[errorCode];
  const minutes = kind && limits?.[kind];
  if (typeof minutes !== "number" || !Number.isInteger(minutes) || minutes <= 0) return "";
  const formatted = minutes.toLocaleString(locale);
  return locale === "zh-CN"
    ? `（当前配置为 ${formatted} 分钟）`
    : ` (current limit is ${formatted} ${minutes === 1 ? "minute" : "minutes"})`;
}
