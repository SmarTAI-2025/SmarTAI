import type { TaskStatus } from "./task";

export type GradingAggregationMethod = "single" | "weighted_average" | "judge_agent";
export type GradingKnowledgeScope = "none" | "all_task_docs";
export type GradingFeedbackTone = "encouraging" | "neutral" | "strict";
export type GradingFeedbackLength = "short" | "medium" | "long";
export type GradingFeedbackLanguage = "zh" | "en";

export interface GradingSetup {
  schema_version: 1;
  selected_provider_ids: string[];
  primary_provider_id: string;
  aggregation_method: GradingAggregationMethod;
  multi_sample_n: number;
  knowledge_scope: GradingKnowledgeScope;
  strictness: number;
  allow_partial_credit: boolean;
  feedback_tone: GradingFeedbackTone;
  feedback_length: GradingFeedbackLength;
  feedback_language: GradingFeedbackLanguage;
  suggest_corrections: boolean;
  low_confidence_threshold: number;
  teacher_notes: string;
}

export interface GradingSetupExpert {
  provider_id: string;
  provider_type: string;
  model: string;
  display_name?: string | null;
  enabled: boolean;
  scope: "owner" | "shared" | string;
  is_shared: boolean;
  editable: boolean;
  max_concurrent: number;
  rpm: number;
}

export interface GradingSetupKnowledgeDocument {
  doc_id: string;
  filename: string;
  chunk_count: number;
  uploaded_at: number;
}

export interface GradingSetupKnowledge {
  scope_options: GradingKnowledgeScope[];
  task_doc_count: number;
  task_docs: GradingSetupKnowledgeDocument[];
}

export interface GradingSetupReadiness {
  ready: boolean;
  blocking_issues: string[];
  warnings: string[];
}

export interface GradingSetupResponse {
  task_id: string;
  task_status: TaskStatus;
  workflow_revision: number;
  configured: boolean;
  grading_setup: GradingSetup | null;
  suggested_setup: GradingSetup | null;
  grading_setup_fingerprint: string | null;
  grading_setup_updated_at: number | null;
  available_experts: GradingSetupExpert[];
  knowledge: GradingSetupKnowledge;
  readiness: GradingSetupReadiness;
  status?: "saved" | "unchanged";
}

export interface SaveGradingSetupInput {
  taskId: string;
  expectedWorkflowRevision: number;
  gradingSetup: GradingSetup;
}
