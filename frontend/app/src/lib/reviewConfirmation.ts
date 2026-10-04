import type { PreparationIssue, ProblemInfo, StudentAnswerInfo, Task } from "@/types";
/** Optional defaults are information, not recognition defects. */
export function questionIssueNeedsReview(issue: PreparationIssue): boolean {
  if (issue.code === "recognition_partial" && onlyUnverifiedTargets(issue)) return false;
  return issue.status === "open" && (issue.severity === "blocking" ||
    (issue.severity !== "info" && issue.code !== "default_max_score_requires_review"));
}

function onlyUnverifiedTargets(issue: PreparationIssue): boolean {
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  return Boolean(coverage?.unverified_targets?.length && !issue.details?.error_code
    && !(issue.details?.confidence_reasons as string[] | undefined)?.includes("low_confidence_content")
    && !["failed_pages", "unprocessed_pages", "missing_targets"].some(key => coverage?.[key]?.length));
}

export function recognitionIssueLabel(issue: PreparationIssue, locale: string): string | null {
  if (!["recognition_partial", "recognition_needs_review"].includes(issue.code)) return null;
  const zh = locale === "zh-CN";
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  const reasons = [
    ["failed_pages", "识别失败的页码", "Pages that failed recognition"],
    ["unprocessed_pages", "尚未处理的页码", "Unprocessed pages"],
    ["missing_targets", "未找到的题号", "Question numbers not found"],
  ].flatMap(([key, cn, en]) => coverage?.[key]?.length ? [`${zh ? cn : en}：${coverage[key].join(", ")}`] : []);
  if (reasons.length) return reasons.join("；");
  if (onlyUnverifiedTargets(issue)) return zh
    ? "已定位题号；请对照原文件确认题干完整。这是常规确认，尚未发现具体缺失。"
    : "Question numbers were located. Confirm the complete wording against the source; no specific omission was detected.";
  return zh ? "部分转写内容可信度较低，请对照原文件检查公式、符号和条件。" : "Some transcription has low confidence. Check formulas, symbols and conditions against the source.";
}

export function submissionRecognitionIncomplete(task?: Pick<Task, "submission_source_summary" | "submission_sources">): boolean {
  return Boolean(task?.submission_source_summary?.failed || task?.submission_source_summary?.pending
    || task?.submission_sources?.some(source => source.status === "failed" || source.status === "processing"));
}
export function questionReviewConfirmed(problem: ProblemInfo): boolean {
  return problem.review_status === "confirmed" && !questionReviewBlocked(problem) &&
    problem.max_score_review_status === "confirmed" &&
    !(problem.preparation_issues ?? []).some(issue => issue.status === "open");
}
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
