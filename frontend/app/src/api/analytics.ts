import { deleteJSON, getJSON, postJSON } from "./client";
import type { AnalyticsMode, AnalyticsResult, PerQuestionBreakdown } from "@/types";

export function runAnalyticsQuery(
  taskId: string,
  question: string,
  mode: AnalyticsMode,
): Promise<AnalyticsResult> {
  return postJSON<AnalyticsResult>(`/analytics/${taskId}/query`, { question, mode });
}

export function getPerQuestionBreakdown(taskId: string, qId: string): Promise<PerQuestionBreakdown> {
  return getJSON<PerQuestionBreakdown>(`/analytics/${taskId}/per_question/${qId}`);
}

export function resetPerQuestionCache(taskId: string, qId: string): Promise<{ status: "cleared" }> {
  return deleteJSON<{ status: "cleared" }>(`/analytics/${taskId}/per_question/${qId}/cache`);
}
