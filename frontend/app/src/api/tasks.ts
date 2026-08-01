import { deleteJSON, getBlob, getJSON, postJSON, postMultipart, putJSON, type UploadOptions } from "./client";
import type {
  CorrectionReviewResponse,
  GenerateResultArtifactsResponse,
  HistoryInterpretation,
  ProblemInfo,
  ResultArtifactIndex,
  StudentAnswerInfo,
  StudentIdentityUpdateResponse,
  SubmissionIdentityMode,
  Task,
  TaskHistoryQuery,
  TaskHistoryResponse,
  TaskFinalizationResponse,
  TaskLite,
  TaskListResponse,
  TaskMetadataPatch,
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

export interface CreateTaskInput extends Omit<TaskMetadataPatch, "name"> {
  name: string;
  idempotencyKey: string;
}

export function createTask({ idempotencyKey, ...body }: CreateTaskInput): Promise<TaskLite> {
  return postJSON<TaskLite, TaskMetadataPatch>("/tasks/", body, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

export function listTasks(): Promise<TaskListResponse> {
  return getJSON<TaskListResponse>("/tasks/");
}

export function listTaskHistory(query: TaskHistoryQuery): Promise<TaskHistoryResponse> {
  return getJSON<TaskHistoryResponse>("/tasks/", {
    params: {
      page: query.page,
      page_size: query.page_size,
      q: query.q || undefined,
      semester_id: query.semester_id || undefined,
      course_id: query.course_id || undefined,
      tag_ids: query.tag_ids?.length ? query.tag_ids.join(",") : undefined,
      statuses: query.statuses?.length ? query.statuses.join(",") : undefined,
      unfinished: query.unfinished || undefined,
      needs_attention: query.needs_attention || undefined,
      sort: query.sort,
    },
  });
}

export function getTask(taskId: string): Promise<Task> {
  return getJSON<Task>(`/tasks/${taskId}`);
}

export function updateTask(taskId: string, patch: TaskMetadataPatch): Promise<TaskLite> {
  return putJSON<TaskLite, TaskMetadataPatch>(`/tasks/${taskId}`, patch);
}

export function interpretTaskHistoryQuery(query: string): Promise<HistoryInterpretation> {
  return postJSON<HistoryInterpretation, { query: string }>("/tasks/query/interpret", { query });
}

export function deleteTask(taskId: string): Promise<{ status: string }> {
  return deleteJSON<{ status: string }>(`/tasks/${taskId}`);
}

export function extractProblems(taskId: string, file: File, options?: UploadOptions): Promise<TaskMutationResponse> {
  return postMultipart<TaskMutationResponse>(`/tasks/${taskId}/extract_problems`, file, options);
}

export interface ParseSubmissionsInput extends UploadOptions {
  taskId: string;
  file: File;
  identityMode?: SubmissionIdentityMode;
  rosterFile?: File | null;
  recognitionProviderId?: string | null;
  replaceConfirmed?: boolean;
}

export function parseSubmissions({
  taskId,
  file,
  identityMode = "filename",
  rosterFile,
  recognitionProviderId,
  replaceConfirmed = false,
  ...options
}: ParseSubmissionsInput): Promise<TaskMutationResponse> {
  return postMultipart<TaskMutationResponse>(`/tasks/${taskId}/parse_submissions`, file, {
    ...options,
    fields: {
      ...options.fields,
      identity_mode: identityMode,
      recognition_provider_id: recognitionProviderId,
      replace_confirmed: replaceConfirmed,
    },
    files: {
      ...options.files,
      roster_file: rosterFile,
    },
  });
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

export function getTaskFinalization(taskId: string): Promise<TaskFinalizationResponse> {
  return getJSON<TaskFinalizationResponse>(`/tasks/${encodeURIComponent(taskId)}/finalization`);
}

export function confirmTaskFinalization(
  taskId: string,
  expectedWorkflowRevision: number,
): Promise<TaskFinalizationResponse> {
  return postJSON<TaskFinalizationResponse>(
    `/tasks/${encodeURIComponent(taskId)}/finalization/confirm`,
    { expected_workflow_revision: expectedWorkflowRevision },
  );
}

export function getTaskResultArtifacts(taskId: string): Promise<ResultArtifactIndex> {
  return getJSON<ResultArtifactIndex>(`/tasks/${encodeURIComponent(taskId)}/artifacts`);
}

export function generateTaskResultArtifacts(
  taskId: string,
  expectedWorkflowRevision: number,
): Promise<GenerateResultArtifactsResponse> {
  return postJSON<GenerateResultArtifactsResponse>(
    `/tasks/${encodeURIComponent(taskId)}/artifacts/generate`,
    { expected_workflow_revision: expectedWorkflowRevision },
  );
}

export function getTaskResultArtifactBlob(
  taskId: string,
  version: number,
  artifactId: string,
): Promise<Blob> {
  return getBlob(`/tasks/${encodeURIComponent(taskId)}/artifacts/${version}/${encodeURIComponent(artifactId)}`);
}

export function updateProblem(
  taskId: string,
  qId: string,
  patch: Pick<Partial<ProblemInfo>, "stem" | "criterion" | "review_status" | "reference_answer" | "solution_code" | "test_cases">,
): Promise<{ status: "ok"; q_id: string; problem: ProblemInfo }> {
  return putJSON(`/tasks/${taskId}/problems/${qId}`, patch);
}

export function updateStudentAnswer(
  taskId: string,
  studentId: string,
  qId: string,
  patch: Pick<Partial<StudentAnswerInfo>, "content" | "flag" | "review_status"> & { expected_workflow_revision?: number },
): Promise<{ status: "ok"; stu_id: string; q_id: string; answer: StudentAnswerInfo; workflow_revision: number }> {
  return putJSON(`/tasks/${taskId}/students/${studentId}/answers/${qId}`, patch);
}

export function updateStudentIdentity(
  taskId: string,
  currentStudentId: string,
  input: {
    expected_workflow_revision: number;
    student_id: string;
    student_name: string;
  },
): Promise<StudentIdentityUpdateResponse> {
  return putJSON(`/tasks/${taskId}/students/${currentStudentId}/identity`, input);
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

export function updateCorrectionReview(
  taskId: string,
  studentId: string,
  qId: string,
  input: {
    expected_workflow_revision: number;
    teacher_score: number;
    teacher_comment: string;
    confirm: boolean;
  },
): Promise<CorrectionReviewResponse> {
  return putJSON<CorrectionReviewResponse>(
    `/tasks/${encodeURIComponent(taskId)}/reviews/${encodeURIComponent(studentId)}/${encodeURIComponent(qId)}`,
    input,
  );
}
