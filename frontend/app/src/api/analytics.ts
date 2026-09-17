import { deleteJSON, getJSON, postJSON } from "./client";
import type { AnalyticsMode, AnalyticsResult, FilterIntentResult, FilterIntentSurface, PerQuestionBreakdown } from "@/types";

export const ANALYTICS_REQUEST_TIMEOUT_MS = 300_000;

export function runAnalyticsQuery(
  taskId: string,
  question: string,
  mode: AnalyticsMode,
): Promise<AnalyticsResult> {
  return postJSON<AnalyticsResult>(`/analytics/${taskId}/query`, { question, mode }, { timeout: ANALYTICS_REQUEST_TIMEOUT_MS });
}

export function interpretFilterIntent(
  taskId: string,
  question: string,
  surface: FilterIntentSurface,
  signal?: AbortSignal,
): Promise<FilterIntentResult> {
  return postJSON<FilterIntentResult>(`/analytics/${taskId}/filter-intent`, { question, surface }, { timeout: ANALYTICS_REQUEST_TIMEOUT_MS, ...(signal ? { signal } : {}) });
}

export function getPerQuestionBreakdown(taskId: string, qId: string): Promise<PerQuestionBreakdown> {
  return getJSON<PerQuestionBreakdown>(`/analytics/${taskId}/per_question/${qId}`);
}

export function resetPerQuestionCache(taskId: string, qId: string): Promise<{ status: "cleared" }> {
  return deleteJSON<{ status: "cleared" }>(`/analytics/${taskId}/per_question/${qId}/cache`);
}
