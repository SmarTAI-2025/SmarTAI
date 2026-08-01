import type { KBDoc } from "./kb";
import type { JobProgress } from "./progress";
import type { AICompletionTarget } from "./aiCompletions";

export type TaskStatus =
  | "draft"
  | "extracting_problems"
  | "problems_ready"
  | "parsing_submissions"
  | "submissions_ready"
  | "grading"
  | "graded"
  | "review_confirmed"
  | "generating_analysis"
  | "finalized"
  | "error";

export interface TestCase {
  input: string;
  expected_output: string;
  description: string;
  title?: string;
  visibility?: "example" | "hidden";
  purpose?: "normal" | "boundary" | "error" | "performance" | "other";
  io_mode?: "stdin" | "function";
  source: "teacher" | "llm_generated";
  sandbox_feasible: boolean;
  function_name?: string | null;
  function_args?: unknown[] | null;
  expected_return?: string | null;
}

export interface ProblemInfo {
  q_id: string;
  number: string;
  type: string;
  stem: string;
  criterion: string;
  review_status?: "needs_review" | "edited" | "confirmed";
  reference_answer?: string | null;
  solution_code?: string | null;
  test_cases?: TestCase[] | null;
  material_provenance?: Partial<Record<"criterion" | "reference_answer" | "test_cases", MaterialFieldProvenance>>;
  ai_completion_provenance?: Partial<Record<AICompletionTarget, AICompletionProvenance>>;
  preparation_issues?: PreparationIssue[];
}

export interface PreparationIssue {
  issue_id: string;
  q_id?: string | null;
  field: "stem" | "answer" | "rubric" | "programming_tests" | "source";
  code: "low_confidence" | "source_conflict" | "ai_source_conflict" | "ambiguous_question_match" | "unmapped_source_content" | "parse_anomaly" | "generation_failed" | "rubric_step_reference_conflict" | "invalid_test_case" | "reference_solution_failed_case";
  severity: "info" | "warning" | "blocking";
  source_ids?: string[];
  details?: Record<string, unknown>;
  status: "open" | "acknowledged" | "resolved";
}

export interface MaterialFieldProvenance {
  import_job_id: string;
  candidate_id: string;
  source_kind: "upload" | "library" | "inline_text";
  source_filename: string;
  library_material_id?: string | null;
  confidence: number;
  match_status: "exact" | "possible";
  source_excerpt: string;
  source_location: string;
  reason: string;
  review_status: "pending" | "edited" | "confirmed";
  imported_at: number;
  updated_at: number;
}

export interface AICompletionProvenance {
  job_id: string;
  candidate_id: string;
  source_kind: "ai_generated";
  provider_id: string;
  review_status: "pending" | "edited" | "confirmed";
  generated_at: number;
  updated_at: number;
}

export interface StudentAnswerInfo {
  q_id: string;
  number: string;
  type: string;
  content: string;
  flag: string[];
  review_status?: "pending" | "confirmed";
}

export interface StudentSubmission {
  stu_id: string;
  stu_name: string;
  stu_ans: StudentAnswerInfo[];
  source_filename?: string | null;
  identity_match_method?: SubmissionIdentityMode | null;
  identity_status?: "matched" | "needs_review";
}

export interface StudentIdentityUpdateResponse {
  status: "ok";
  previous_student_id: string;
  student: StudentSubmission;
  workflow_revision: number;
}

export type SubmissionIdentityMode = "filename" | "roster" | "manual_review";

export interface StepScore {
  step_no: number;
  desc: string;
  is_correct: boolean;
  score: number;
}

export interface ExpertResult {
  provider: string;
  score: number;
  max_score: number;
  confidence: number;
  comment: string;
  steps: StepScore[];
  hits?: string[] | null;
  logs?: string | null;
  raw_output?: string | null;
  duration_ms?: number | null;
  error_kind?: "quota_exhausted" | "transient_llm" | "parse_failed" | "general" | string | null;
}

export interface Correction {
  q_id: string;
  type: string;
  score: number;
  max_score: number;
  confidence: number;
  comment: string;
  steps: StepScore[];
  hits?: string[] | null;
  logs?: string | null;
  expert_results: ExpertResult[];
  synthesis_method?: string | null;
  is_score?: number | null;
  requires_human_review: boolean;
  review_reasons: string[];
  teacher_score?: number | null;
  teacher_comment?: string;
  review_status?: "pending" | "edited" | "confirmed";
  reviewed_at?: number | null;
}

export interface StudentResult {
  student_id: string;
  student_name?: string;
  corrections: Correction[];
  student_answers?: StudentAnswerInfo[];
}

export interface TaskLite {
  task_id: string;
  name: string;
  owner_id: string;
  status: TaskStatus;
  workflow_revision: number;
  semester_id?: string | null;
  course_id?: string | null;
  tag_ids?: string[];
  needs_attention?: boolean;
  extract_job_id?: string | null;
  parse_job_id?: string | null;
  grading_job_id?: string | null;
  final_result_version?: number;
  final_result_updated_at?: number | null;
  final_result_dirty?: boolean;
  analysis_status?: "not_generated" | "generating" | "ready" | "stale";
  analysis_result_version?: number | null;
  analysis_generated_at?: number | null;
  analysis_error?: string | null;
  last_failed_job_id?: string | null;
  problem_file_name?: string | null;
  submission_file_name?: string | null;
  pending_submission_file_name?: string | null;
  submission_identity_mode?: SubmissionIdentityMode;
  submission_roster_name?: string | null;
  submission_recognition_provider_id?: string | null;
  reference_file_name?: string | null;
  test_cases_file_name?: string | null;
  reference_parse_job_id?: string | null;
  test_cases_parse_job_id?: string | null;
  ai_completion_job_id?: string | null;
  last_ai_completion_job_id?: string | null;
  ai_completion_error?: string | null;
  grading_setup_configured?: boolean;
  problem_count: number;
  student_count: number;
  kb_docs: Record<string, KBDoc>;
  kb_doc_count: number;
  error?: string | null;
  created_at: number;
  updated_at: number;
}

export interface Task extends TaskLite {
  problem_data: Record<string, ProblemInfo>;
  student_data: Record<string, StudentSubmission>;
}

export interface TaskStateSnapshot extends TaskLite {
  progress?: JobProgress | null;
  active_job_id?: string | null;
  active_operation?:
    | "question_preparation"
    | "problem_extraction"
    | "submission_recognition"
    | "grading"
    | "material_import"
    | "ai_completion"
    | null;
}

export type TaskListResponse = Record<string, TaskLite>;

export interface TaskMetadataPatch {
  name?: string | null;
  semester_id?: string | null;
  course_id?: string | null;
  tag_ids?: string[];
}

export interface TaskMutationResponse {
  status: "started" | "already_running" | "already_done" | "success" | "ok" | string;
  task_id?: string;
  job_id?: string;
  unchanged?: boolean;
  problem_count?: number;
  student_count?: number;
  file_count?: number;
}

export interface TaskResultResponse {
  status: "completed" | TaskStatus | "not_found";
  task_id: string;
  results?: StudentResult[];
  problem_data?: Record<string, ProblemInfo>;
  student_data?: Record<string, StudentSubmission>;
  timestamp?: number;
  error?: string | null;
}

export interface TeacherCommentsResponse {
  comments: Record<string, string>;
}

export interface TeacherCommentResponse {
  status: "ok";
  student_id: string;
  q_id: string;
  teacher_comment: string;
}

export interface CorrectionReviewResponse {
  status: "ok";
  unchanged: boolean;
  student_id: string;
  q_id: string;
  correction: Correction;
  workflow_revision: number;
}

export interface RequiredResultReview {
  student_id: string;
  q_id: string;
  reasons: string[];
  confirmed: boolean;
}

export interface TaskFinalizationResponse {
  status?: "ok" | "already_done";
  unchanged?: boolean;
  task_id: string;
  task_status: TaskStatus;
  workflow_revision: number;
  ready_for_confirmation: boolean;
  required_review_count: number;
  confirmed_required_count: number;
  remaining_review_count: number;
  remaining_reviews: RequiredResultReview[];
  final_result_version: number;
  final_result_updated_at?: number | null;
  final_result_dirty: boolean;
  analysis_status: "not_generated" | "generating" | "ready" | "stale";
  analysis_result_version?: number | null;
  analysis_generated_at?: number | null;
  analysis_error?: string | null;
  available_result_versions: number;
}

export type ResultArtifactStatus = "not_generated" | "ready" | "stale" | "historical";

export interface ResultArtifactFile {
  artifact_id: "grades_csv" | "learning_report_md" | "published_answers_md" | "published_answers_tex" | "formal_result_json";
  title: string;
  title_en: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
}

export interface ResultArtifactVersion {
  version: number;
  current: boolean;
  status: ResultArtifactStatus;
  confirmed_at?: number | null;
  generated_at?: number | null;
  files: ResultArtifactFile[];
}

export interface ResultArtifactIndex {
  task_id: string;
  current_result_version: number;
  analysis_status: TaskFinalizationResponse["analysis_status"];
  analysis_result_version?: number | null;
  versions: ResultArtifactVersion[];
}

export interface GenerateResultArtifactsResponse extends Omit<TaskFinalizationResponse, "status"> {
  status: "ok" | "already_done";
  unchanged: boolean;
  artifacts: ResultArtifactIndex;
}
