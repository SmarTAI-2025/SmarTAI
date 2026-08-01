import type { JobProgress } from "./progress";
import type { ProblemCandidateSummary, ProblemLibraryMaterial, ProblemStructureMode } from "./problemSources";
import type { TestCase } from "./task";

export type MaterialImportTarget = "criterion" | "reference_answer" | "test_cases";
export type MaterialImportSourceMode = "upload" | "library";
export type MaterialImportStatus = "running" | "ready" | "applied" | "error";

export interface MaterialImportSource {
  kind: MaterialImportSourceMode;
  filename: string;
  size_bytes?: number;
  sha256?: string;
  library_material_id?: string | null;
}

export interface MaterialImportPreflightInput {
  taskId: string;
  file?: File | null;
  libraryMaterialId?: string | null;
  targets: MaterialImportTarget[];
  structureMode: ProblemStructureMode;
  extractionHint: string;
  saveToLibrary: boolean;
}

export interface MaterialImportPreflightResponse {
  status: "ready";
  source_token: string;
  source: MaterialImportSource;
  targets: MaterialImportTarget[];
  structure_mode: ProblemStructureMode;
  extraction_hint: string;
  candidate_summary: ProblemCandidateSummary;
  base_workflow_revision: number;
  workflow_revision: number;
  expires_at?: number;
  saved_material?: ProblemLibraryMaterial & { created: boolean };
}

export interface MaterialImportStartResponse {
  status: "started" | "already_running" | "plan_ready" | "already_done";
  job_id: string;
  task_id: string;
  request_fingerprint?: string;
  workflow_revision: number;
}

export interface MaterialImportCandidate {
  candidate_id: string;
  q_id: string;
  target: MaterialImportTarget;
  match_status: "exact" | "possible";
  text_value?: string | null;
  test_cases?: TestCase[] | null;
  confidence: number;
  source_excerpt: string;
  source_location: string;
  reason: string;
  would_overwrite: boolean;
}

export interface MaterialImportSummary {
  candidate_count?: number;
  conflict_count?: number;
  low_confidence_count?: number;
  exact_match_count?: number;
  possible_match_count?: number;
  skipped_unknown_qid?: number;
  skipped_invalid?: number;
  skipped_non_programming?: number;
  by_target?: Partial<Record<MaterialImportTarget, number>>;
  applied_candidate_ids?: string[];
  [key: string]: unknown;
}

export interface MaterialImportPlanResponse {
  job_id: string;
  task_id: string;
  status: MaterialImportStatus;
  request_fingerprint: string;
  source: MaterialImportSource;
  targets: MaterialImportTarget[];
  structure_mode: ProblemStructureMode;
  extraction_hint: string;
  overwrite_policy: "missing_only";
  candidates: MaterialImportCandidate[];
  summary: MaterialImportSummary;
  progress?: JobProgress | null;
  error?: string | null;
  applied_candidate_ids: string[];
  workflow_revision: number;
  created_at: number;
  completed_at?: number | null;
  expires_at: number;
  storage: "memory" | string;
}

export interface ApplyMaterialImportInput {
  taskId: string;
  jobId: string;
  acceptedCandidateIds: string[];
  overwriteCandidateIds: string[];
  expectedWorkflowRevision: number;
}

export interface ApplyMaterialImportResponse {
  status: "applied" | "already_done";
  job_id: string;
  task_id: string;
  summary: MaterialImportSummary;
  workflow_revision: number;
}
