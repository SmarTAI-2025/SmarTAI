import type { ProblemInfo, StudentAnswerInfo, Task } from "@/types";
export interface ReviewBlocker { label: string; href: string }
export function questionReviewBlocked(problem: ProblemInfo): boolean {
  return !problem.stem?.trim() || (problem.preparation_issues ?? []).some(issue => issue.status === "open" && issue.severity === "blocking");
}
export function answerReviewBlocked(answer?: StudentAnswerInfo): boolean {
  return Boolean(answer?.flag?.some(flag => ["recognition_failed", "parse_failed", "no_matching_answer"].includes(flag)));
}
export function submissionReviewBlockers(task: Task | undefined, locale: string, studentId?: string): ReviewBlocker[] {
  if (!task) return [];
  const zh = locale === "zh-CN";
  const student = Object.values(task.student_data ?? {}).find(item => item.stu_id === studentId);
  const sources = (task.submission_sources ?? []).filter(source => (!studentId || source.source_id === student?.source_id || source.student_candidate === studentId) && (source.status === "failed" || source.status === "processing"));
  const sourceIssues = sources.map(source => ({ label: `${source.file_name} · ${zh ? "识别未完成，请重试或修正原文件" : "Recognition incomplete; retry or correct the source"}`, href: `/tasks/${task.task_id}/submissions/upload#source-${encodeURIComponent(source.source_id)}` }));
  const answers = Object.values(task.student_data ?? {}).filter(item => !studentId || item.stu_id === studentId).flatMap(item => item.stu_ans.filter(answerReviewBlocked).map(answer => ({ label: `${item.stu_name || item.stu_id} · ${answer.number || answer.q_id} · ${zh ? "识别失败" : "Recognition failed"}`, href: `/tasks/${task.task_id}/students/${encodeURIComponent(item.stu_id)}?question=${encodeURIComponent(answer.q_id)}` })));
  return [...sourceIssues, ...answers];
}
