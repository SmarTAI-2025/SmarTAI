export type ProgressPhase =
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
}

export interface JobProgress {
  contract_version?: number;
  job_id?: string | null;
  phase: ProgressPhase;
  total_students: number;
  total_questions: number;
  completed_units: number;
  active: ActiveUnit[];
  messages: ProgressEvent[];
  error_detail?: string | null;
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
