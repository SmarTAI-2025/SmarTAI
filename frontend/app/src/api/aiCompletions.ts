import { getJSON, postJSON } from "./client";
import type {
  AICompletionJobResponse,
  AICompletionPreflightResponse,
  StartAICompletionInput,
  StartAICompletionResponse,
} from "@/types";

export function getAICompletionPreflight(taskId: string) {
  return getJSON<AICompletionPreflightResponse>(`/tasks/${taskId}/ai-completions/preflight`);
}

export function startAICompletion({
  taskId,
  targetIds,
  expectedWorkflowRevision,
  testCaseCount,
}: StartAICompletionInput) {
  return postJSON<StartAICompletionResponse, {
    target_ids: string[];
    expected_workflow_revision: number;
    test_case_count?: number;
  }>(`/tasks/${taskId}/ai-completions/confirm`, {
    target_ids: targetIds,
    expected_workflow_revision: expectedWorkflowRevision,
    ...(typeof testCaseCount === "number" ? { test_case_count: testCaseCount } : {}),
  });
}

export function getAICompletionJob(taskId: string, jobId: string) {
  return getJSON<AICompletionJobResponse>(`/tasks/${taskId}/ai-completions/${jobId}`);
}
