export type ProgressPhase =
  | "paused"
  | "pending"
  | "ingesting"
  | "extracting"
  | "parsing"
  | "classifying"
  | "grading"
  | "reviewing"
  | "aggregating"
  | "done"
  | "error";

export interface ActiveUnit {
  student_id: string;
  q_id: string;
  skill: string;
  expert?: string | null;
  step: string;
}

export interface ProgressEvent {
  ts: number;
  level: "info" | "warn" | "error";
  message: string;
  unit?: ActiveUnit | null;
  recognition?: RecognitionRequestState | null;
}

export interface RecognitionRequestState {
  count?: number;
  state: "queued" | "responding" | "retrying";
  since?: number;
  attempt?: number;
  max_attempts?: number;
  retry_at?: number;
  reason?: "rate_limit" | "timeout" | "unavailable";
}

export interface ProviderFailure {
  code: "provider_upstream_unavailable" | "provider_timeout" | "provider_unreachable" | "provider_rate_limited" | "provider_response_invalid";
  http_status?: number | null;
}

export interface JobProgress {
  recognition_requests?: RecognitionRequestState[];
  model_progress?: GradingModelProgress[];
  model_eta_seconds?: number | null;
  running_units?: number;
  activity_latest?: GradingActivityEvent | null;
  activity_cursor?: number;
  contract_version?: number;
  job_id?: string | null;
  phase: ProgressPhase;
  total_students: number;
  total_questions: number;
  completed_units: number;
  active: ActiveUnit[];
  messages: ProgressEvent[];
  error_detail?: string | null;
  provider_failures?: ProviderFailure[];
  model_waits?: Array<{ model: string; reason: "provider_rate_limited"; attempt: number; max_attempts: number; retry_at: number }>;
  recognition_failure?: { failed_pages?: number[]; processed_pages?: number[] };
  started_at?: number | null;
  workflow?: string | null;
  stage_sequence?: string[];
  current_step?: string | null;
  total_steps?: number | null;
  completed_steps?: number | null;
  stage_metrics?: Record<string, number>;
  question_labels?: Record<string, string>;
  question_error_codes?: Record<string, string>;
  completed_question_ids?: string[];
  active_question_ids?: string[];
  failed_question_ids?: string[];
  last_activity_at?: number | null;
}

export interface GradingModelProgress {
  id: string;
  label: string;
  total: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  processing: number;
  queued: number;
  responding: number;
  retrying: number;
  completion_times: number[];
}

export interface GradingActivityEvent {
  sequence: number;
  ts: number;
  code: "processing" | "queued" | "responding" | "retrying" | "succeeded" | "failed" | "cancelled" | "started" | "completed" | "stopped";
  model_id?: string;
  attempt?: number;
  max_attempts?: number;
  retry_at?: number;
  reason?: "rate_limit" | "timeout" | "unavailable";
}
