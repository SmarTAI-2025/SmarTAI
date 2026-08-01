import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as edu from "@/api/education";
import type { GradingRun } from "@/types/education";
import {
  assignmentKeys,
  educationCourseKeys,
  gradingRunKeys,
  resultKeys,
  submissionKeys,
} from "./keys";

// ─── courses ────────────────────────────────────────────────────────────────

export function useCourses() {
  return useQuery({ queryKey: educationCourseKeys.list(), queryFn: edu.listCourses });
}

export function useCourse(courseId: string | undefined) {
  return useQuery({
    queryKey: courseId
      ? educationCourseKeys.detail(courseId)
      : ["education", "courses", "detail", "none"],
    queryFn: () => edu.getCourse(courseId!),
    enabled: Boolean(courseId),
  });
}

export function useCreateCourse() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: edu.createCourse,
    onSuccess: () => qc.invalidateQueries({ queryKey: educationCourseKeys.all }),
  });
}

export function useEnrollStudents() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ courseId, studentIds }: { courseId: string; studentIds: string[] }) =>
      edu.enrollStudents(courseId, studentIds),
    onSuccess: () => qc.invalidateQueries({ queryKey: educationCourseKeys.all }),
  });
}

export function useDeleteCourse() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: edu.deleteCourse,
    onSuccess: () => qc.invalidateQueries({ queryKey: educationCourseKeys.all }),
  });
}

// ─── assignments + questions ────────────────────────────────────────────────

export function useAssignments(courseId?: string) {
  return useQuery({
    queryKey: courseId ? assignmentKeys.list(courseId) : assignmentKeys.all,
    queryFn: () => edu.listAssignments(courseId),
  });
}

export function useAssignment(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? assignmentKeys.detail(assignmentId)
      : ["education", "assignments", "detail", "none"],
    queryFn: () => edu.getAssignment(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}

export function useCreateAssignment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: edu.createAssignment,
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: assignmentKeys.list(data.course_id) });
    },
  });
}

export function useQuestions(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? assignmentKeys.questions(assignmentId)
      : ["education", "assignments", "questions", "none"],
    queryFn: () => edu.listQuestions(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}

export function useAddQuestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, input }: { assignmentId: string; input: Parameters<typeof edu.addQuestion>[1] }) =>
      edu.addQuestion(assignmentId, input),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: assignmentKeys.questions(vars.assignmentId) });
      qc.invalidateQueries({ queryKey: assignmentKeys.detail(vars.assignmentId) });
    },
  });
}

export function useImportQuestionsFile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, file }: { assignmentId: string; file: File }) =>
      edu.importQuestionsFile(assignmentId, file),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: assignmentKeys.questions(vars.assignmentId) });
      qc.invalidateQueries({ queryKey: assignmentKeys.detail(vars.assignmentId) });
    },
  });
}

export function usePublishAssignment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, expectedVersion }: { assignmentId: string; expectedVersion: number }) =>
      edu.publishAssignment(assignmentId, expectedVersion),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: assignmentKeys.detail(data.id) });
      qc.invalidateQueries({ queryKey: assignmentKeys.list(data.course_id) });
    },
  });
}

// ─── submissions ────────────────────────────────────────────────────────────

export function useSubmissions(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? submissionKeys.list(assignmentId)
      : ["education", "submissions", "list", "none"],
    queryFn: () => edu.listSubmissions(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}

export function useSubmitOnline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, answers }: { assignmentId: string; answers: { q_id: string; content: string }[] }) =>
      edu.submitOnline(assignmentId, answers),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: submissionKeys.list(vars.assignmentId) });
    },
  });
}

export function useUploadSubmission() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, file }: { assignmentId: string; file: File }) =>
      edu.uploadSubmission(assignmentId, file),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: submissionKeys.list(vars.assignmentId) });
    },
  });
}

export function useTeacherUploadSubmission() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      assignmentId,
      studentId,
      file,
    }: {
      assignmentId: string;
      studentId: string;
      file: File;
    }) => edu.teacherUploadSubmission(assignmentId, studentId, file),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: submissionKeys.list(vars.assignmentId) });
    },
  });
}

export function useTeacherImport() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, items }: { assignmentId: string; items: Parameters<typeof edu.teacherImport>[1] }) =>
      edu.teacherImport(assignmentId, items),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: submissionKeys.list(vars.assignmentId) });
    },
  });
}

// ─── grading runs + review + release ────────────────────────────────────────

export function useGradingRuns(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? gradingRunKeys.list(assignmentId)
      : ["education", "grading-runs", "list", "none"],
    queryFn: () => edu.listGradingRuns(assignmentId!),
    enabled: Boolean(assignmentId),
    refetchInterval: (query) => {
      const runs = query.state.data as GradingRun[] | undefined;
      return runs?.some((run) => run.status === "queued" || run.status === "running") ? 1_000 : false;
    },
  });
}

export function useStartGradingRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: edu.startGradingRun,
    onSuccess: (_data, assignmentId) => {
      qc.invalidateQueries({ queryKey: gradingRunKeys.list(assignmentId) });
    },
  });
}

export function useReviewQueue(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? gradingRunKeys.reviewQueue(assignmentId)
      : ["education", "grading-runs", "review", "none"],
    queryFn: () => edu.reviewQueue(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}

export function useReviewGradeResult() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      gradeResultId,
      newScore,
      newComment,
    }: {
      gradeResultId: string;
      newScore: number;
      newComment: string;
    }) => edu.reviewGradeResult(gradeResultId, newScore, newComment),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: resultKeys.all });
      qc.invalidateQueries({ queryKey: gradingRunKeys.all });
    },
  });
}

export function useReleaseGradingRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: edu.releaseGradingRun,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: gradingRunKeys.all });
      qc.invalidateQueries({ queryKey: resultKeys.all });
    },
  });
}

// ─── results ────────────────────────────────────────────────────────────────

export function useTeacherSummary(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? resultKeys.summary(assignmentId)
      : ["education", "results", "summary", "none"],
    queryFn: () => edu.teacherSummary(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}

export function usePerQuestionAggregates(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? resultKeys.perQuestion(assignmentId)
      : ["education", "results", "per-question", "none"],
    queryFn: () => edu.perQuestionAggregates(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}

export function useMyStudentResult(assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentId
      ? resultKeys.me(assignmentId)
      : ["education", "results", "me", "none"],
    queryFn: () => edu.studentResult(assignmentId!),
    enabled: Boolean(assignmentId),
  });
}
