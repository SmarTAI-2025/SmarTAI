import { getJSON, putJSON } from "./client";
import type { GradingSetupResponse, SaveGradingSetupInput } from "@/types";

export function getGradingSetup(taskId: string): Promise<GradingSetupResponse> {
  return getJSON<GradingSetupResponse>(`/tasks/${taskId}/grading-setup`);
}

export function saveGradingSetup({
  taskId,
  expectedWorkflowRevision,
  gradingSetup,
}: SaveGradingSetupInput): Promise<GradingSetupResponse> {
  return putJSON<GradingSetupResponse>(`/tasks/${taskId}/grading-setup`, {
    expected_workflow_revision: expectedWorkflowRevision,
    grading_setup: gradingSetup,
  });
}
