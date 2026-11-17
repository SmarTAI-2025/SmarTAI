import type { JobProgress } from "./progress";

export type AICompletionTarget =
  | "criterion"
  | "reference_answer"
  | "solution_code"
  | "test_cases";

export interface AICompletionMissingTarget {
  target_id: string;
  q_id: string;
  question_number: string;
  question_type: string;
  target: AICompletionTarget;
  label: string;
}

export type AICompletionTargetCounts = Record<AICompletionTarget, number>;

export interface AICompletionPreflightResponse {
  status: "ready";
  task_id: string;
  overwrite_policy: "missing_only";
  missing_targets: AICompletionMissingTarget[];
  summary: {
    question_count: number;
    missing_count: number;
    by_target: AICompletionTargetCounts;
  };
  workflow_revision: number;
  provider_call_performed: false;
  storage: "memory" | string;
}

export interface StartAICompletionInput {
  taskId: string;
  targetIds: string[];
  expectedWorkflowRevision: number;
  testCaseCount?: number;
}

export interface StartAICompletionResponse {
  status: "started" | "already_running" | "already_done";
  job_id: string;
  task_id: string;
  request_fingerprint: string;
  workflow_revision: number;
}

export interface AICompletionJobResponse {
  job_id: string;
  task_id: string;
  status: "running" | "done" | "error";
  overwrite_policy: "missing_only";
  target_ids: string[];
  summary: {
    requested_count: number;
    generated_count: number;
    applied_count: number;
    skipped_count: number;
    invalid_count: number;
    by_target: AICompletionTargetCounts;
  };
  applied_target_ids: string[];
  skipped_target_ids: string[];
  error?: string | null;
  progress?: JobProgress | null;
  workflow_revision: number;
  created_at: number;
  completed_at?: number | null;
  expires_at: number;
  storage: "memory" | string;
}
