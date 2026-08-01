/**
 * Normalized education API contracts shared by the admin/teacher/student
 * workspaces. These mirror the FastAPI response shapes in backend/api/{courses,
 * assignments,submissions,grading_runs,results,admin}.ts; the frontend branches
 * on the stable status unions (never on natural-language strings).
 */

export type AssignmentStatus = "draft" | "ready" | "published" | "closed" | "archived";

export type GradingRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "partial_failed"
  | "failed"
  | "cancelled";

export type GradeResultStatus = "graded" | "failed" | "needs_review";

export interface Course {
  id: string;
  name: string;
  code: string;
  description: string;
  teacher_id: string;
  student_ids: string[];
  student_count: number;
  created_at: number;
  updated_at: number;
}

export interface Assignment {
  id: string;
  course_id: string;
  teacher_id: string;
  name: string;
  description: string;
  status: AssignmentStatus;
  due_at: number | null;
  created_at: number;
  updated_at: number;
  published_at: number | null;
  version: number;
  question_count: number;
}

export interface Question {
  id: string;
  assignment_id: string;
  q_id: string;
  order_index: number;
  number: string;
  type: string;
  stem: string;
  criterion: string;
  max_score: number;
  reference_answer: string | null;
  test_cases: unknown[] | null;
  source: Record<string, unknown> | null;
  version: number;
  created_at: number;
  updated_at: number;
}

export interface SubmissionSummary {
  id: string;
  assignment_id: string;
  student_id: string;
  current_revision_id: string | null;
  current_revision_number: number | null;
  created_at: number;
  updated_at: number;
}

export interface SubmissionAnswer {
  id: string;
  revision_id: string;
  question_id: string;
  q_id: string;
  number: string;
  type: string;
  content: string;
  flag: string[];
}

export interface SubmissionRevision {
  id: string;
  submission_id: string;
  revision_number: number;
  source: "online" | "teacher_import";
  file_name: string;
  created_at: number;
  answers: SubmissionAnswer[];
}

export interface GradingRun {
  id: string;
  assignment_id: string;
  teacher_id: string;
  status: GradingRunStatus;
  lease_owner: string | null;
  lease_expiry: number | null;
  last_heartbeat_at: number | null;
  total_submissions: number;
  completed_submissions: number;
  failed_submissions: number;
  error_message: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  released_at: number | null;
}

export interface GradeResult {
  id: string;
  grading_run_id: string;
  question_id: string;
  q_id: string;
  student_id: string;
  ai_score: number | null;
  ai_max_score: number;
  ai_comment: string;
  result_status: GradeResultStatus;
  requires_review: boolean;
  review_reason: string | null;
  score: number | null;
  teacher_comment: string;
  effective_score?: number | null;
  effective_comment?: string;
  teacher_review?: {
    id: string;
    grade_result_id: string;
    teacher_id: string;
    previous_score: number | null;
    previous_comment: string | null;
    new_score: number;
    new_comment: string;
    created_at: number;
  } | null;
}

export interface AdminUser {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  is_active: boolean;
  created_at: number;
}

export interface Invite {
  code: string;
  email: string | null;
  role: UserRole;
  course_id: string | null;
  created_at: number;
  expires_at: number;
  used_at: number | null;
  used_by: string | null;
}

/** Stable error envelope: every normalized router returns this shape on failure. */
export interface DomainErrorResponse {
  error: {
    code: string;
    message: string;
  };
}

import type { UserRole } from "./auth";
export type { UserRole };
