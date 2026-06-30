import { deleteJSON, getJSON, postJSON, postMultipart, putJSON, type UploadOptions } from "./client";
import type {
  ProblemInfo,
  StudentAnswerInfo,
  Task,
  TaskLite,
  TaskListResponse,
  TaskMutationResponse,
  TaskResultResponse,
  TaskStateSnapshot,
  TeacherCommentResponse,
  TeacherCommentsResponse,
} from "@/types";

// The React UI intentionally does not expose a grading-language control.
// Current backend compatibility: GradeRequest.language still accepts "en" /
// "zh" only in practice because non-"en" builds a Chinese system prompt.
// When backend-side auto language detection lands, this constant is the only
// frontend API compatibility point that should change.
const BACKEND_COMPAT_GRADING_LANGUAGE = "en";

export function buildGradePayload(options: { multiSampleN?: number | null } = {}) {
  const payload: { language: string; multi_sample_n?: number } = {
    language: BACKEND_COMPAT_GRADING_LANGUAGE,
  };

  if (typeof options.multiSampleN === "number" && options.multiSampleN > 1) {
    payload.multi_sample_n = Math.floor(options.multiSampleN);
  }

  return payload;
}

export function createTask(name: string): Promise<TaskLite> {
  return postJSON<TaskLite, { name: string }>("/tasks/", { name });
}

export function listTasks(): Promise<TaskListResponse> {
  return getJSON<TaskListResponse>("/tasks/");
}

export function getTask(taskId: string): Promise<Task> {
  return getJSON<Task>(`/tasks/${taskId}`);
}

export function updateTask(taskId: string, patch: { name?: string | null }): Promise<TaskLite> {
  return putJSON<TaskLite, { name?: string | null }>(`/tasks/${taskId}`, patch);
}

export function deleteTask(taskId: string): Promise<{ status: string }> {
  return deleteJSON<{ status: string }>(`/tasks/${taskId}`);
}

export function extractProblems(taskId: string, file: File, options?: UploadOptions): Promise<TaskMutationResponse> {
  return postMultipart<TaskMutationResponse>(`/tasks/${taskId}/extract_problems`, file, options);
}

export function parseSubmissions(taskId: string, file: File, options?: UploadOptions): Promise<TaskMutationResponse> {
  return postMultipart<TaskMutationResponse>(`/tasks/${taskId}/parse_submissions`, file, options);
}

export function uploadReference(taskId: string, file: File, options?: UploadOptions): Promise<TaskMutationResponse> {
  return postMultipart<TaskMutationResponse>(`/tasks/${taskId}/upload_reference`, file, options);
}

export function uploadTestCases(taskId: string, file: File, options?: UploadOptions): Promise<TaskMutationResponse> {
  return postMultipart<TaskMutationResponse>(`/tasks/${taskId}/upload_test_cases`, file, options);
}

export function startGrading(
  taskId: string,
  options: { multiSampleN?: number | null } = {},
): Promise<TaskMutationResponse> {
  return postJSON<TaskMutationResponse>(`/tasks/${taskId}/grade`, buildGradePayload(options));
}

export function getTaskState(taskId: string): Promise<TaskStateSnapshot> {
  return getJSON<TaskStateSnapshot>(`/tasks/${taskId}/state`);
}

export function getTaskResult(taskId: string): Promise<TaskResultResponse> {
  return getJSON<TaskResultResponse>(`/tasks/${taskId}/result`);
}

export function updateProblem(
  taskId: string,
  qId: string,
  patch: Pick<Partial<ProblemInfo>, "stem" | "criterion">,
): Promise<{ status: "ok"; q_id: string; problem: ProblemInfo }> {
  return putJSON(`/tasks/${taskId}/problems/${qId}`, patch);
}

export function updateStudentAnswer(
  taskId: string,
  studentId: string,
  qId: string,
  patch: Pick<Partial<StudentAnswerInfo>, "content" | "flag">,
): Promise<{ status: "ok"; stu_id: string; q_id: string; answer: StudentAnswerInfo }> {
  return putJSON(`/tasks/${taskId}/students/${studentId}/answers/${qId}`, patch);
}

export function setTeacherComment(
  taskId: string,
  studentId: string,
  qId: string,
  comment: string,
): Promise<TeacherCommentResponse> {
  return postJSON<TeacherCommentResponse>(`/tasks/${taskId}/teacher_comment`, {
    student_id: studentId,
    q_id: qId,
    comment,
  });
}

export function listTeacherComments(taskId: string): Promise<TeacherCommentsResponse> {
  return getJSON<TeacherCommentsResponse>(`/tasks/${taskId}/teacher_comments`);
}
