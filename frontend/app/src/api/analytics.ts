import { EMPTY_FILTER_INTENT } from "@/lib/taskFilterIntent";
import { isGroundedExecution } from "@/lib/groundedAsk";
import type { GroundedAskExecution } from "@/types";
import { deleteJSON, getJSON, postJSON } from "./client";
import type { AnalyticsMode, AnalyticsResult, FilterIntentResult, FilterIntentSurface, PerQuestionBreakdown } from "@/types";

// Model-backed operations can outlast the ordinary 30-second data-request limit.
export const ANALYTICS_REQUEST_TIMEOUT_MS = 300_000;

export function runAnalyticsQuery(
  taskId: string,
  question: string,
  mode: AnalyticsMode,
  history?: string[],
): Promise<AnalyticsResult> {
  return postJSON<AnalyticsResult>(`/analytics/${taskId}/query`, { question, mode, ...(history?.length ? { history: history.slice(-4) } : {}) }, { timeout: ANALYTICS_REQUEST_TIMEOUT_MS });
}

export interface AskContext { studentId?: string; history?: string[] }
export async function runGroundedAsk(taskId: string | undefined, question: string, surface: string,
  signal?: AbortSignal, context?: AskContext): Promise<GroundedAskExecution> {
  const endpoint = taskId ? `/analytics/${encodeURIComponent(taskId)}/ask` : "/analytics/ask";
  const result = await postJSON<GroundedAskExecution>(endpoint, { question, surface,
    ...(context?.studentId ? { context_student_id: context.studentId } : {}),
    ...(context?.history?.length ? { history: context.history.slice(-4) } : {}),
  }, { timeout: ANALYTICS_REQUEST_TIMEOUT_MS, ...(signal ? { signal } : {}) });
  if (!isGroundedExecution(result)) throw new Error("Invalid Ask query result");
  return result;
}
export async function interpretFilterIntent(taskId: string, question: string,
  surface: FilterIntentSurface, signal?: AbortSignal, context?: AskContext): Promise<FilterIntentResult> {
  const execution = await runGroundedAsk(taskId, question, surface, signal, context);
  return { ...EMPTY_FILTER_INTENT, recognized: execution.recognized, explanation: execution.explanation, execution };
}

export function getPerQuestionBreakdown(taskId: string, qId: string): Promise<PerQuestionBreakdown> {
  return getJSON<PerQuestionBreakdown>(`/analytics/${taskId}/per_question/${qId}`, { timeout: ANALYTICS_REQUEST_TIMEOUT_MS });
}

export function resetPerQuestionCache(taskId: string, qId: string): Promise<{ status: "cleared" }> {
  return deleteJSON<{ status: "cleared" }>(`/analytics/${taskId}/per_question/${qId}/cache`);
}
