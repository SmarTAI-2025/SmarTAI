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
  phase: ProgressPhase;
  total_students: number;
  total_questions: number;
  completed_units: number;
  active: ActiveUnit[];
  messages: ProgressEvent[];
  error_detail?: string | null;
}
