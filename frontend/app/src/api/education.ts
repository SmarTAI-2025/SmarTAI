/**
 * Resource API clients for the normalized education workflow. Each function maps
 * to one FastAPI endpoint and surfaces the stable DomainError envelope via
 * APIError; callers branch on `error.payload.error.code`, never on message text.
 */
import { apiClient } from "./client";
import type {
  Assignment,
  Course,
  GradingRun,
  GradeResult,
  Question,
  SubmissionRevision,
  SubmissionSummary,
} from "@/types/education";

// ─── courses ────────────────────────────────────────────────────────────────

export async function listCourses(): Promise<Course[]> {
  const { data } = await apiClient.get<Course[]>("/courses");
  return data;
}

export async function createCourse(input: { name: string; code?: string; description?: string }): Promise<Course> {
  const { data } = await apiClient.post<Course>("/courses", input);
  return data;
}

export async function getCourse(courseId: string): Promise<Course> {
  const { data } = await apiClient.get<Course>(`/courses/${courseId}`);
  return data;
}

export async function enrollStudents(courseId: string, studentIds: string[]): Promise<Course> {
  const { data } = await apiClient.post<{ student_ids: string[] }>(`/courses/${courseId}/enroll`, {
    student_ids: studentIds,
  });
  return data as unknown as Course;
}

export async function deleteCourse(courseId: string): Promise<void> {
  await apiClient.delete(`/courses/${courseId}`);
}

// ─── assignments + questions ────────────────────────────────────────────────

export async function listAssignments(courseId?: string): Promise<Assignment[]> {
  const { data } = await apiClient.get<Assignment[]>("/assignments", { params: courseId ? { course_id: courseId } : {} });
  return data;
}

export async function getAssignment(assignmentId: string): Promise<Assignment> {
  const { data } = await apiClient.get<Assignment>(`/assignments/${assignmentId}`);
  return data;
}

export async function createAssignment(input: {
  course_id: string;
  name: string;
  description?: string;
  due_at?: number | null;
}): Promise<Assignment> {
  const { data } = await apiClient.post<Assignment>("/assignments", input);
  return data;
}

export async function listQuestions(assignmentId: string): Promise<Question[]> {
  const { data } = await apiClient.get<Question[]>(`/assignments/${assignmentId}/questions`);
  return data;
}

export async function addQuestion(assignmentId: string, input: {
  q_id: string;
  order_index?: number;
  type: string;
  stem?: string;
  max_score?: number;
  reference_answer?: string | null;
  test_cases?: unknown[] | null;
}): Promise<Question> {
  const { data } = await apiClient.post<Question>(`/assignments/${assignmentId}/questions`, input);
  return data;
}

export async function importQuestionsFile(assignmentId: string, file: File): Promise<Question[]> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await apiClient.post<Question[]>(
    `/assignments/${assignmentId}/questions/import-file`,
    form,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return data;
}

export async function publishAssignment(assignmentId: string, expectedVersion: number): Promise<Assignment> {
  const { data } = await apiClient.post<Assignment>(`/assignments/${assignmentId}/publish`, {
    expected_version: expectedVersion,
  });
  return data;
}

export async function closeAssignment(assignmentId: string, expectedVersion: number): Promise<Assignment> {
  const { data } = await apiClient.post<Assignment>(`/assignments/${assignmentId}/close`, {
    expected_version: expectedVersion,
  });
  return data;
}

// ─── submissions ─────────────────────────────────────────────────────────────

export async function submitOnline(assignmentId: string, answers: { q_id: string; content: string }[]): Promise<SubmissionRevision> {
  const { data } = await apiClient.post<SubmissionRevision>("/submissions/submit", {
    assignment_id: assignmentId,
    answers,
  });
  return data;
}

export async function teacherImport(
  assignmentId: string,
  items: { student_id: string; file_name?: string; answers: { q_id: string; content: string }[] }[],
): Promise<{ succeeded: string[]; failed: { student_id: string; error: string }[] }> {
  const { data } = await apiClient.post("/submissions/teacher-import", { assignment_id: assignmentId, items });
  return data;
}

export async function listSubmissions(assignmentId: string): Promise<SubmissionSummary[]> {
  const { data } = await apiClient.get<SubmissionSummary[]>(`/submissions/assignment/${assignmentId}`);
  return data;
}

export async function uploadSubmission(assignmentId: string, file: File): Promise<SubmissionRevision> {
  const form = new FormData();
  form.append("assignment_id", assignmentId);
  form.append("file", file);
  const { data } = await apiClient.post<SubmissionRevision>("/submissions/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function teacherUploadSubmission(
  assignmentId: string,
  studentId: string,
  file: File,
): Promise<SubmissionRevision> {
  const form = new FormData();
  form.append("assignment_id", assignmentId);
  form.append("student_id", studentId);
  form.append("file", file);
  const { data } = await apiClient.post<SubmissionRevision>("/submissions/teacher-upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

// ─── grading runs ───────────────────────────────────────────────────────────

export async function startGradingRun(assignmentId: string): Promise<GradingRun> {
  const { data } = await apiClient.post<GradingRun>("/grading-runs", { assignment_id: assignmentId });
  return data;
}

export async function getGradingRun(runId: string): Promise<{ run: GradingRun; events: unknown[] }> {
  const { data } = await apiClient.get(`/grading-runs/${runId}`);
  return data;
}

export async function listGradingRuns(assignmentId: string): Promise<GradingRun[]> {
  const { data } = await apiClient.get<GradingRun[]>(`/grading-runs/by-assignment/${assignmentId}`);
  return data;
}

export async function reviewGradeResult(gradeResultId: string, newScore: number, newComment: string): Promise<unknown> {
  const { data } = await apiClient.post(`/grading-runs/results/${gradeResultId}/review`, {
    new_score: newScore,
    new_comment: newComment,
  });
  return data;
}

export async function releaseGradingRun(runId: string): Promise<GradingRun> {
  const { data } = await apiClient.post<GradingRun>(`/grading-runs/${runId}/release`);
  return data;
}

// ─── results ────────────────────────────────────────────────────────────────

export async function teacherSummary(assignmentId: string): Promise<unknown> {
  const { data } = await apiClient.get(`/results/assignment/${assignmentId}/summary`);
  return data;
}

export async function perQuestionAggregates(assignmentId: string): Promise<unknown> {
  const { data } = await apiClient.get(`/results/assignment/${assignmentId}/questions`);
  return data;
}

export async function reviewQueue(assignmentId: string): Promise<GradeResult[]> {
  const { data } = await apiClient.get<GradeResult[]>(`/results/assignment/${assignmentId}/review-queue`);
  return data;
}

export async function studentResult(assignmentId: string): Promise<GradeResult[]> {
  const { data } = await apiClient.get<GradeResult[]>(`/results/assignment/${assignmentId}/me`);
  return data;
}
