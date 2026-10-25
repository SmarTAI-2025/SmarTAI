import type { PreparationIssue, ProblemInfo, StudentAnswerInfo, StudentSubmission, SubmissionSourceOutcome, Task } from "@/types";
import { confidenceValue, confidenceNeedsAttention } from "./confidencePolicy";
import { formatSubmissionFlag, hasQuestionType, questionIssueCodeLabel } from "./reviewLabels";

export type ReviewAttention = "none" | "advisory" | "blocking";
export function questionConfidence(issue: PreparationIssue, problem?: ProblemInfo): number | null {
  const explicit = confidenceValue(issue.details?.confidence);
  if (explicit !== null) return explicit;
  const field = issue.field === "answer" ? "reference_answer" : issue.field === "rubric" ? "criterion" : issue.field === "programming_tests" ? "test_cases" : null;
  return confidenceValue(field ? problem?.material_provenance?.[field]?.confidence : undefined);
}
function questionHasLowConfidence(issue: PreparationIssue, problem?: ProblemInfo): boolean {
  return confidenceNeedsAttention(questionConfidence(issue, problem));
}
export function questionIssueSeverity(issue: PreparationIssue, problem?: ProblemInfo): ReviewAttention {
  if (questionHasMissingEvidence(issue)) return issue.field === "answer" ? "advisory" : "blocking";
  if (issue.status !== "open") return "none";
  if (["low_confidence", "recognition_partial", "recognition_needs_review"].includes(issue.code)) {
    return questionHasLowConfidence(issue, problem) ? "advisory" : "none";
  }
  if (issue.field === "answer") return "advisory";
  if (issue.code === "manual_completion_required") {
    const fields = issue.details?.required_fields;
    if (Array.isArray(fields) && fields.some(field => ["number", "type", "stem", "criterion"].includes(String(field)))) {
      return problem && !manualQuestionFieldsMissing(issue, problem) ? "none" : "blocking";
    }
    return "advisory";
  }
  if (issue.severity === "blocking" || ["manual_completion_required", "source_conflict", "ambiguous_question_match"].includes(issue.code)) return "blocking";
  return issue.severity === "warning" ? "advisory" : "none";
}

function questionHasMissingEvidence(issue: PreparationIssue): boolean {
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  return ["recognition_partial", "recognition_needs_review"].includes(issue.code) && issue.status !== "resolved"
    && (issue.details?.incomplete === true || issue.severity === "blocking"
      || Boolean(issue.details?.error_code) || ["failed_pages", "unprocessed_pages", "missing_targets"].some(key => Boolean(coverage?.[key]?.length)));
}

/** Qualitative OCR labels are not numeric probabilities or failures. */
export function questionIssueNeedsReview(issue: PreparationIssue): boolean {
  return questionIssueSeverity(issue) !== "none";
}

function onlyUnverifiedTargets(issue: PreparationIssue): boolean {
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  return Boolean(coverage?.unverified_targets?.length && !issue.details?.error_code
    && !(issue.details?.confidence_reasons as string[] | undefined)?.includes("low_confidence_content")
    && !["failed_pages", "unprocessed_pages", "missing_targets"].some(key => coverage?.[key]?.length));
}

export function recognitionIssueLabel(issue: PreparationIssue, locale: string): string | null {
  if (issue.details?.review_display_reason === "missing_type") return locale === "zh-CN"
    ? "请选择题型。" : "Select the question type.";
  if (issue.details?.review_display_reason === "missing_number") return locale === "zh-CN"
    ? "请补齐题号。" : "Enter the question number.";
  if (issue.code === "source_conflict" && issue.details?.reason === "test_case_output_conflict") return locale === "zh-CN"
    ? "测试用例中，同一输入对应了不同预期输出，请核对。"
    : "Test cases have different expected outputs for the same input. Please review.";
  if (issue.code === "math_rubric_advisory") return locale === "zh-CN"
    ? "本题评分标准有待核对的内容。" : "Some rubric details need review.";
  if (issue.details?.review_display_reason === "empty_stem") return locale === "zh-CN"
    ? "题干为空，请对照原件补齐。" : "Question text is empty. Complete it against the source.";
  if (issue.details?.review_display_reason === "missing_rubric") return locale === "zh-CN"
    ? "评分标准缺失或不完整，请补齐。" : "The rubric is missing or incomplete. Complete it.";
  if (issue.details?.review_display_reason === "missing_answer") return locale === "zh-CN"
    ? "标答缺失或不完整，建议补充或核对确认。" : "The reference answer is missing or incomplete; review suggested.";
  if (issue.details?.review_display_reason === "rubric_conflict") return locale === "zh-CN"
    ? "小问分值缺失、重复或合计与大题满分冲突，请修正。"
    : "Subpart points are missing, duplicated, or inconsistent with the question maximum. Correct them.";
  if (issue.code === "manual_completion_required" && questionIssueSeverity(issue) === "advisory") return locale === "zh-CN"
    ? "部分补充资料未完成，建议核对。" : "Some supplementary materials are incomplete; review suggested.";
  if (issue.code === "manual_completion_required") return locale === "zh-CN"
    ? "请补齐缺失资料。"
    : "Complete the missing materials.";
  if (issue.code === "ambiguous_question_match") return locale === "zh-CN"
    ? "资料无法唯一匹配到本题，请对照原件核对内容。"
    : "The source cannot be uniquely matched to this question. Check the content against the original.";
  if (issue.code === "unmapped_source_content") return locale === "zh-CN"
    ? "原文件中有内容尚未匹配，请核对本题是否完整。"
    : "Some source content is unmatched. Check whether this question is complete.";
  if (!["recognition_partial", "recognition_needs_review", "low_confidence"].includes(issue.code) && !questionHasLowConfidence(issue)) return null;
  const zh = locale === "zh-CN";
  const coverage = issue.details?.coverage as Record<string, unknown[]> | undefined;
  const reasons = [
    ["failed_pages", "识别失败的页码", "Pages that failed recognition"],
    ["unprocessed_pages", "尚未处理的页码", "Unprocessed pages"],
    ["missing_targets", "未找到的题号", "Question numbers not found"],
  ].flatMap(([key, cn, en]) => coverage?.[key]?.length ? [`${zh ? cn : en}：${coverage[key].join(", ")}`] : []);
  if (reasons.length) return reasons.join("；");
  if (issue.details?.error_code) return zh
    ? "来源读取失败，请重试或修正原件后继续。"
    : "Source reading failed. Retry or correct the source before continuing.";
  if (issue.code === "recognition_partial" && (issue.details?.incomplete === true || issue.severity === "blocking")) return zh
    ? "来源内容不完整，请对照原件补齐。"
    : "Source content is incomplete. Complete it against the original.";
  if (questionHasLowConfidence(issue)) return zh
    ? `置信度 ${questionConfidence(issue)} 较低，建议核对确认。`
    : `Low confidence (${questionConfidence(issue)}); review suggested.`;
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
    !(problem.preparation_issues ?? []).some(issue => issue.status === "open" && issue.code !== "math_rubric_advisory");
}
export interface ReviewBlocker { label: string; href: string }
export function questionReviewBlocked(problem: ProblemInfo): boolean {
  if (!problem.number?.trim() || !hasQuestionType(problem.type) || !problem.stem?.trim() || !problem.criterion?.trim() || problem.rubric_point_summary?.is_valid === false) return true;
  return questionReviewIssues(problem).some(issue => {
    if (issue.severity !== "blocking") return false;
    if (questionHasMissingEvidence(issue)) return true;
    if (issue.code === "manual_completion_required") return manualQuestionFieldsMissing(issue, problem);
    if (["source_conflict", "ambiguous_question_match"].includes(issue.code)) {
      return (problem.preparation_issues ?? []).find(original => original.issue_id === issue.issue_id)?.severity === "blocking";
    }
    return true;
  });
}

function manualQuestionFieldsMissing(issue: PreparationIssue, problem: ProblemInfo): boolean {
  const fields = issue.details?.required_fields;
  return Array.isArray(fields) && fields.some((field: unknown) => field === "type" ? !hasQuestionType(problem.type)
    : (field === "number" || field === "stem" || field === "criterion") && !problem[field]?.trim());
}

export function questionReviewNextBlocked(problem: ProblemInfo): boolean {
  return questionReviewIssues(problem).some(issue => issue.severity === "blocking");
}

export function questionReviewIssues(problem: ProblemInfo): PreparationIssue[] {
  const confirmed = problem.review_status === "confirmed";
  const issues: PreparationIssue[] = (problem.preparation_issues ?? []).flatMap(issue => {
    const severity = questionIssueSeverity(issue, problem);
    if (severity === "none" || (confirmed && severity === "advisory" && issue.code !== "math_rubric_advisory")) return [];
    const confidence = questionConfidence(issue, problem);
    return [{ ...issue, severity: severity === "blocking" ? "blocking" as const : "warning" as const,
      details: { ...issue.details, ...(confidence !== null ? { confidence } : {}) } }];
  });
  function add(field: PreparationIssue["field"], code: PreparationIssue["code"], reason: string, severity: PreparationIssue["severity"] = "blocking") {
    if (issues.some(issue => issue.field === field && issue.severity === severity
      && (field !== "source" || issue.details?.review_display_reason === reason))) return;
    issues.push({ issue_id: `review:${problem.q_id}:${reason}`, q_id: problem.q_id, field, code,
      severity, status: "open", details: { review_display_reason: reason } });
  }
  if (!problem.number?.trim()) add("source", "manual_completion_required", "missing_number");
  if (!hasQuestionType(problem.type)) add("source", "manual_completion_required", "missing_type");
  if (!problem.stem?.trim()) add("stem", "parse_anomaly", "empty_stem");
  if (!problem.criterion?.trim()) add("rubric", "manual_completion_required", "missing_rubric");
  if (problem.rubric_point_summary?.is_valid === false) add("rubric", "rubric_step_reference_conflict", "rubric_conflict");
  if (!confirmed && !problem.reference_answer?.trim()) add("answer", "generation_failed", "missing_answer", "warning");
  return issues.sort((a, b) => Number(b.severity === "blocking") - Number(a.severity === "blocking"));
}

/** Detail copy derives missing fields from the current row, not stale failure markers. */
export function questionReviewNotes(problem: ProblemInfo, locale: string, fallbackLabel: (code: string) => string, splitMissingFields = false) {
  const issues = questionReviewIssues(problem);
  const missing = [
    [!problem.number?.trim(), "题号", "question number"],
    [!hasQuestionType(problem.type), "题型", "question type"],
    [!problem.stem?.trim(), "题干", "question text"],
    [!problem.criterion?.trim(), "评分标准", "rubric"],
  ] as const;
  const names = missing.filter(([absent]) => absent).map(([, zh, en]) => locale === "zh-CN" ? zh : en);
  const answerMissing = !problem.reference_answer?.trim();
  const notes = issues.filter(issue => !(splitMissingFields && ["parse_anomaly", "generation_failed"].includes(issue.code)
    && ((issue.field === "rubric" && !problem.criterion?.trim()) || (issue.field === "stem" && !problem.stem?.trim()))))
    .filter(issue => !((names.length || answerMissing) && (issue.code === "manual_completion_required"
    || ["missing_number", "missing_type", "empty_stem", "missing_rubric", "missing_answer"].includes(String(issue.details?.review_display_reason)))))
    .map(issue => ({ label: recognitionIssueLabel(issue, locale) ?? fallbackLabel(issue.code),
      severity: issue.severity === "blocking" ? "blocking" as const : "advisory" as const }));
  for (const fields of splitMissingFields ? names.map(name => [name]) : names.length ? [names] : []) {
    notes.push({ label: locale === "zh-CN"
      ? `请补齐缺失的${fields.join("、")}。`
      : `Complete the missing ${fields.join(", ")}.`,
      severity: "blocking" });
  }
  if (answerMissing && problem.review_status !== "confirmed") {
    notes.push({ label: locale === "zh-CN"
      ? "缺少标答（可选），可按需补充。"
      : "The reference answer is optional; add one if helpful.", severity: "advisory" });
  }
  // Identical failure labels are one message, with the strongest severity.
  const unique = new Map<string, typeof notes[number]>();
  for (const note of notes) if (!unique.has(note.label) || note.severity === "blocking") unique.set(note.label, note);
  return [...unique.values()].sort((a, b) => Number(b.severity === "blocking") - Number(a.severity === "blocking"));
}

export function questionReviewBlockers(problems: ProblemInfo[], taskId: string, locale: string): ReviewBlocker[] {
  if (!problems.length) return [{ label: locale === "zh-CN" ? "请先建立题目行并填写内容" : "Create question rows and enter their content first", href: `/tasks/${taskId}/questions#question-count` }];
  return problems.flatMap(problem => questionReviewNotes(problem, locale,
    code => questionIssueCodeLabel(code, locale), true)
    .filter(note => note.severity === "blocking").map(note => ({
      label: `${locale === "zh-CN" ? `第 ${problem.number || problem.q_id} 题` : `Question ${problem.number || problem.q_id}`} · ${note.label}`,
      href: `/tasks/${taskId}/questions/${encodeURIComponent(problem.q_id)}/content#question-${encodeURIComponent(problem.q_id)}`,
    })));
}

export function answerMetadataConfirmationAllowed(answer?: StudentAnswerInfo): boolean {
  // The server recomputes this marker from saved evidence, never client flags.
  if (!answer?.content?.trim() || !answer.flag?.includes("recognition_metadata_review")) return false;
  const fields = answer.flag.filter(flag => flag.startsWith("recognition_invalid_fields:"))
    .flatMap(flag => flag.split(":", 2)[1].split(","));
  return fields.length > 0 && fields.every(field => ["number", "type", "flag", "recognition_confidence"].includes(field))
    && !answer.flag.some(flag => ["parse_failed", "no_matching_answer", "recognition_incomplete", "recognition_paused", "provider_submit_uncertain"].includes(flag)
      || flag.startsWith("recognition_failed_pages:") || flag.startsWith("recognition_unprocessed_pages:"));
}
export function answerReviewBlocked(answer?: StudentAnswerInfo): boolean {
  if (answer?.flag?.includes("answer_assignment_conflict")) return true;
  if (answer?.content?.trim() && answer.flag?.includes("recognition_matching_review")
    && !answer.flag.some(flag => ["parse_failed", "no_matching_answer", "recognition_incomplete", "recognition_paused", "provider_submit_uncertain"].includes(flag)
      || flag.startsWith("recognition_failed_pages:") || flag.startsWith("recognition_unprocessed_pages:") || flag.startsWith("recognition_invalid_fields:"))) return false;
  if (answerMetadataConfirmationAllowed(answer)) return false;
  return Boolean(answer?.flag?.some(flag => ["recognition_failed", "parse_failed", "no_matching_answer", "recognition_manual_pending"].includes(flag)
    || flag.startsWith("recognition_invalid_fields:")));
}
export function answerReviewNextBlocked(answer?: StudentAnswerInfo): boolean {
  return answerReviewBlocked(answer) || Boolean(answer?.flag?.some(flag => ["recognition_matching_review", "recognition_metadata_review"].includes(flag)));
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
  if (!allStudents.length) return [{ label: zh ? "请先添加学生或填写人数建立学生行" : "Add students or enter a student count first", href: `/tasks/${task.task_id}/submissions#student-count` }];
  const student = allStudents.find(item => item.stu_id === studentId);
  const sources = (task.submission_sources ?? []).filter(source => {
    if (studentId && source.source_id !== student?.source_id && source.student_candidate !== studentId) return false;
    if (source.status === "failed" || source.status === "processing") return true;
    const unresolvedIdentity = source.resolution_status !== "identity_resolved" && (source.status === "identity_needs_review" || source.internal_status === "identity_conflict");
    return unresolvedIdentity && !allStudents.some(item => item.source_id === source.source_id || item.stu_id === source.student_candidate);
  });
  const sourceIssues = sources.map(source => ({ label: `${source.file_name} · ${zh ? "识别未完成，请重试或修正原文件" : "Recognition incomplete; retry or correct the source"}`, href: `/tasks/${task.task_id}/submissions/upload#source-${encodeURIComponent(source.source_id)}` }));
  const students = allStudents.filter(item => !studentId || item.stu_id === studentId);
  const identities = students.filter(item => confirming ? studentIdentityInvalid(item) : studentIdentityReviewBlocked(item, task.submission_sources)).flatMap(item => {
    const fields = [
      ...(studentIdentityIssue(item.stu_name) ? [zh ? "学生姓名" : "student name"] : []),
      ...(studentIdentityIssue(item.stu_id) ? [zh ? "学号" : "student ID"] : []),
    ];
    return (fields.length ? fields : [zh ? "学生姓名、学号" : "student name and ID"]).map(field => ({
      label: `${item.stu_name || item.stu_id} · ${zh ? `请修正${field}` : `Correct the ${field}`}`,
      href: `/tasks/${task.task_id}/students/${encodeURIComponent(item.stu_id)}?identity=edit`,
    }));
  });
  const answers = students.flatMap(item => item.stu_ans.filter(confirming ? answerReviewBlocked : answerReviewNextBlocked)
    .flatMap(answer => answerReviewNotes(answer, locale, true).filter(note => note.severity === "blocking").map(note => ({
      label: `${item.stu_name || item.stu_id} · ${zh ? `第 ${answer.number || answer.q_id} 题` : `Question ${answer.number || answer.q_id}`} · ${note.label}`,
      href: `/tasks/${task.task_id}/students/${encodeURIComponent(item.stu_id)}?question=${encodeURIComponent(answer.answer_id ?? answer.q_id)}`,
    }))));
  return [...sourceIssues, ...identities, ...answers];
}

export function answerReviewNotes(answer: StudentAnswerInfo | undefined, locale: string, splitInvalidFields = false): Array<{ label: string; severity: "blocking" | "advisory" }> {
  if (!answer || (answer.review_status === "confirmed" && !answerReviewNextBlocked(answer))) return [];
  const confidence = confidenceValue(answer.recognition_confidence);
  const metadataOnly = answerMetadataConfirmationAllowed(answer);
  const matchingOnly = answer.flag?.includes("recognition_matching_review");
  const flags = (answer.flag ?? []).flatMap(flag => splitInvalidFields && flag.startsWith("recognition_invalid_fields:")
    ? flag.slice("recognition_invalid_fields:".length).split(",").map(field => `recognition_invalid_fields:${field}`) : [flag])
    .filter(flag => !["manual_blank_confirmed", "recognition_needs_review", "recognition_metadata_review", "ocr_only_structure_requires_teacher_review",
    "recognition_very_low_confidence", "very_low_confidence", "recognition_low_confidence", "low_confidence"].includes(flag)
    && !flag.startsWith("recognition_confidence:") && !flag.startsWith("answer_recognition_confidence:")
    && !flag.startsWith("recognition_failed_pages:") && !flag.startsWith("recognition_unprocessed_pages:")
    && !((metadataOnly || matchingOnly) && ["recognition_failed", "recognition_manual_pending"].includes(flag)));
  const required = new Set(["answer_assignment_conflict", "recognition_matching_review", "recognition_failed", "parse_failed", "no_matching_answer", "recognition_manual_pending", "recognition_paused", "recognition_incomplete"]);
  return [
    ...(confidenceNeedsAttention(confidence) ? [{ label: locale === "zh-CN" ? `置信度 ${confidence} 较低，建议核对确认。` : `Low confidence (${confidence}); review suggested.`, severity: "advisory" as const }] : []),
    ...[...new Set(flags)].map(flag => ({ label: formatSubmissionFlag(flag, locale), severity: required.has(flag) || flag.startsWith("recognition_invalid_fields:") ? "blocking" as const : "advisory" as const })),
  ];
}
