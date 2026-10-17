import type { PreparationIssue, ProblemInfo, StudentAnswerInfo, StudentSubmission, SubmissionSourceOutcome, Task } from "@/types";

export type ReviewAttention = "none" | "advisory" | "blocking";
export const VERY_LOW_REVIEW_CONFIDENCE = 0.35;

function questionHasLowConfidence(issue: PreparationIssue): boolean {
  return issue.code === "low_confidence"
    || (Array.isArray(issue.details?.confidence_reasons) && issue.details.confidence_reasons.includes("low_confidence_content"));
}

function questionHasMissingEvidence(issue: PreparationIssue): boolean {
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  return issue.code === "recognition_partial" && issue.status !== "resolved"
    && ["failed_pages", "unprocessed_pages", "missing_targets"].some(key => Boolean(coverage?.[key]?.length));
}

/** Generic source uncertainty is optional; concrete failures and low confidence need review. */
export function questionIssueNeedsReview(issue: PreparationIssue): boolean {
  if (questionHasMissingEvidence(issue)) return true;
  if (issue.status !== "open") return false;
  return issue.severity === "blocking" || questionHasLowConfidence(issue);
}

function onlyUnverifiedTargets(issue: PreparationIssue): boolean {
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  return Boolean(coverage?.unverified_targets?.length && !issue.details?.error_code
    && !(issue.details?.confidence_reasons as string[] | undefined)?.includes("low_confidence_content")
    && !["failed_pages", "unprocessed_pages", "missing_targets"].some(key => coverage?.[key]?.length));
}

export function recognitionIssueLabel(issue: PreparationIssue, locale: string): string | null {
  if (issue.details?.review_display_reason === "empty_stem") return locale === "zh-CN"
    ? "题干为空，请对照原件补齐并确认。" : "Question text is empty. Complete it against the source and confirm.";
  if (issue.details?.review_display_reason === "rubric_conflict") return locale === "zh-CN"
    ? "小问分值缺失、重复或合计与大题满分冲突，请修正并确认。"
    : "Subpart points are missing, duplicated, or inconsistent with the question maximum. Correct and confirm them.";
  if (issue.details?.review_display_reason === "confirmation_required" || issue.code === "required_review_confirmation") return locale === "zh-CN"
    ? "严重问题已修正，请确认本题后继续。" : "The blocking issue was corrected. Confirm this question to continue.";
  if (issue.code === "manual_completion_required") return locale === "zh-CN"
    ? "请补齐缺失资料并确认本题；完成前不能批改。"
    : "Complete the missing materials and confirm this question before grading.";
  if (!["recognition_partial", "recognition_needs_review", "low_confidence"].includes(issue.code) && !questionHasLowConfidence(issue)) return null;
  const zh = locale === "zh-CN";
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  const reasons = [
    ["failed_pages", "识别失败的页码", "Pages that failed recognition"],
    ["unprocessed_pages", "尚未处理的页码", "Unprocessed pages"],
    ["missing_targets", "未找到的题号", "Question numbers not found"],
  ].flatMap(([key, cn, en]) => coverage?.[key]?.length ? [`${zh ? cn : en}：${coverage[key].join(", ")}`] : []);
  if (reasons.length) return reasons.join("；");
  if (questionHasLowConfidence(issue)) return zh
    ? "识别置信度较低，请对照原件核对并确认后继续。"
    : "Recognition confidence is low. Check against the original and confirm before continuing.";
  if (onlyUnverifiedTargets(issue)) return zh
    ? "已定位题号；请对照原文件确认题干完整。这是常规确认，尚未发现具体缺失。"
    : "Question numbers were located. Confirm the complete wording against the source; no specific omission was detected.";
  return zh ? "来源提示可对照原文件核对；尚无本题严重识别错误的证据。" : "The source recommends checking the original; this does not identify a blocking error in this question.";
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
  const required = (problem.preparation_issues ?? []).filter(issue => issue.code === "manual_completion_required")
    .flatMap(issue => (issue.details?.required_fields as (keyof ProblemInfo)[] | undefined) ?? []);
  if (required.some(field => { const value = problem[field]; return !value || (typeof value === "string" && !value.trim()) || (Array.isArray(value) && !value.length); })) return true;
  return !problem.stem?.trim() || problem.rubric_point_summary?.is_valid === false
    || (problem.preparation_issues ?? []).some(issue => questionHasMissingEvidence(issue)
      || (issue.status === "open" && issue.severity === "blocking"));
}

/** Corrected hard failures still require one explicit confirmation before navigation. */
export function questionReviewNextBlocked(problem: ProblemInfo): boolean {
  return questionReviewBlocked(problem) || (problem.review_status !== "confirmed"
    && (problem.preparation_issues ?? []).some(issue => issue.severity === "blocking" || issue.code === "manual_completion_required"
      || questionHasLowConfidence(issue)));
}

export function questionReviewIssues(problem: ProblemInfo): PreparationIssue[] {
  const issues = (problem.preparation_issues ?? []).filter(issue => questionIssueNeedsReview(issue)
    || (problem.review_status !== "confirmed" && questionHasLowConfidence(issue)))
    .map(issue => questionHasMissingEvidence(issue) ? { ...issue, severity: "blocking" as const } : issue);
  function add(field: PreparationIssue["field"], code: PreparationIssue["code"], reason: string) {
    issues.push({ issue_id: `review:${problem.q_id}:${reason}`, q_id: problem.q_id, field, code,
      severity: "blocking", status: "open", details: { review_display_reason: reason } });
  }
  if (!problem.stem?.trim() && !issues.some(issue => issue.field === "stem" && issue.severity === "blocking")) add("stem", "parse_anomaly", "empty_stem");
  if (problem.rubric_point_summary?.is_valid === false && !issues.some(issue => issue.field === "rubric" && issue.severity === "blocking")) add("rubric", "rubric_step_reference_conflict", "rubric_conflict");
  if (questionReviewNextBlocked(problem) && !issues.some(issue => issue.severity === "blocking" || questionHasLowConfidence(issue))) add("source", "manual_completion_required", questionReviewBlocked(problem) ? "missing_material" : "confirmation_required");
  return issues.sort((a, b) => Number(b.severity === "blocking") - Number(a.severity === "blocking"));
}

export function questionReviewBlockers(problems: ProblemInfo[], taskId: string, locale: string): ReviewBlocker[] {
  return problems.filter(questionReviewNextBlocked).map(problem => ({
    label: `${problem.number || problem.q_id} · ${questionReviewIssues(problem).map(issue => recognitionIssueLabel(issue, locale)).filter(Boolean).join("；") || (locale === "zh-CN" ? "请修正严重问题并确认本题" : "Correct the blocking issue and confirm this question")}`,
    href: `/tasks/${taskId}/questions/${encodeURIComponent(problem.q_id)}/content#question-${encodeURIComponent(problem.q_id)}`,
  }));
}
export function answerReviewBlocked(answer?: StudentAnswerInfo): boolean {
  return Boolean(answer?.flag?.some(flag => ["recognition_failed", "parse_failed", "no_matching_answer"].includes(flag)));
}
export function answerReviewNextBlocked(answer?: StudentAnswerInfo): boolean {
  return answerReviewBlocked(answer) || Boolean(answer?.review_status !== "confirmed"
    && (answer?.flag?.includes("recognition_repaired_needs_confirmation") || answerHasLowConfidence(answer)));
}

function answerHasLowConfidence(answer?: StudentAnswerInfo): boolean {
  return Boolean(answer?.flag?.some(flag => ["very_low_confidence", "recognition_low_confidence", "low_confidence"].includes(flag)));
}

export function studentIdentityIssue(value: string): "required" | "placeholder" | null {
  if (!value?.trim()) return "required";
  // Match the ingestion default exactly; do not reject a teacher's edited name.
  return value.trim() === "[Unknown Student]" ? "placeholder" : null;
}

export function studentIdentityInvalid(student: StudentSubmission): boolean {
  return [student.stu_id, student.stu_name].some(value => studentIdentityIssue(value) !== null);
}

export function studentIdentityReviewBlocked(student: StudentSubmission, sources: SubmissionSourceOutcome[] = []): boolean {
  if (studentIdentityInvalid(student)) return true;
  const matching = sources.filter(source => source.source_id === student.source_id || source.student_candidate === student.stu_id);
  return Boolean(student.source_id && student.identity_status !== "matched") || matching.some(source => source.resolution_status !== "identity_resolved"
    && (source.status === "identity_needs_review" || source.internal_status === "identity_conflict"));
}
export function submissionReviewBlockers(task: Task | undefined, locale: string, studentId?: string): ReviewBlocker[] {
  return collectSubmissionBlockers(task, locale, studentId, false);
}

export function submissionConfirmationBlockers(task: Task | undefined, locale: string, studentId?: string): ReviewBlocker[] {
  return collectSubmissionBlockers(task, locale, studentId, true);
}

function collectSubmissionBlockers(task: Task | undefined, locale: string, studentId: string | undefined, confirming: boolean): ReviewBlocker[] {
  if (!task) return [];
  const zh = locale === "zh-CN";
  const allStudents = Object.values(task.student_data ?? {});
  const student = allStudents.find(item => item.stu_id === studentId);
  const sources = (task.submission_sources ?? []).filter(source => {
    if (studentId && source.source_id !== student?.source_id && source.student_candidate !== studentId) return false;
    if (source.status === "failed" || source.status === "processing") return true;
    const unresolvedIdentity = source.resolution_status !== "identity_resolved" && (source.status === "identity_needs_review" || source.internal_status === "identity_conflict");
    return unresolvedIdentity && !allStudents.some(item => item.source_id === source.source_id || item.stu_id === source.student_candidate);
  });
  const sourceIssues = sources.map(source => ({ label: `${source.file_name} · ${zh ? "识别未完成，请重试或修正原文件" : "Recognition incomplete; retry or correct the source"}`, href: `/tasks/${task.task_id}/submissions/upload#source-${encodeURIComponent(source.source_id)}` }));
  const students = allStudents.filter(item => !studentId || item.stu_id === studentId);
  const identities = students.filter(item => confirming ? studentIdentityInvalid(item) : studentIdentityReviewBlocked(item, task.submission_sources)).map(item => ({
    label: `${item.stu_name || item.stu_id} · ${zh ? "请修正并确认学生姓名、学号" : "Correct and confirm the student name and ID"}`,
    href: `/tasks/${task.task_id}/students/${encodeURIComponent(item.stu_id)}?identity=edit`,
  }));
  const answers = students.flatMap(item => item.stu_ans.filter(confirming ? answerReviewBlocked : answerReviewNextBlocked).map(answer => ({ label: `${item.stu_name || item.stu_id} · ${answer.number || answer.q_id} · ${answerReviewBlocked(answer) ? (zh ? "识别失败，请修正并确认" : "Recognition failed; correct and confirm") : answerHasLowConfidence(answer) ? (zh ? "识别置信度较低，请对照原件核对并确认" : "Low recognition confidence; check the original and confirm") : (zh ? "已修正，请确认作答" : "Corrected; confirm the answer")}`, href: `/tasks/${task.task_id}/students/${encodeURIComponent(item.stu_id)}?question=${encodeURIComponent(answer.q_id)}` })));
  return [...sourceIssues, ...identities, ...answers];
}
