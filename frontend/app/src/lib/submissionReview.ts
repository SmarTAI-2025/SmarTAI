import { confidenceNeedsAttention, confidenceValue } from "./confidencePolicy";
import type { ProblemInfo, StudentAnswerInfo, StudentSubmission } from "@/types";
import { answerMetadataConfirmationAllowed, answerReviewBlocked, answerReviewNextBlocked, studentIdentityReviewBlocked, type ReviewAttention } from "./reviewConfirmation";
import type { Locale } from "@/i18n/messages";

export type SubmissionAnswerState = "recognized" | "reviewed" | "flagged" | "empty" | "missing";
export type SubmissionReviewFilter = "all" | "review" | "missing" | "identity";
export type SubmissionReviewSort = "student_id" | "student_name" | "attention";

export interface SubmissionQuestion {
  id: string;
  label: string;
  type: string;
  stem: string;
}

export interface SubmissionReviewStats {
  students: number;
  questions: number;
  expectedCells: number;
  answeredCells: number;
  reviewCells: number;
  identityAnomalies: number;
  identityMatched: number;
}

export interface SubmissionReviewSelection {
  students: StudentSubmission[];
  questions: SubmissionQuestion[];
  explanation: "all" | "student" | "question" | "review" | "missing" | "identity" | "no_match";
  confidenceAlias: boolean;
}

const REVIEW_TOKENS = ["待确认", "pending", "待复核", "需复核", "异常", "有问题", "review", "flagged", "flag"];
const CONFIDENCE_TOKENS = ["低置信", "置信度低", "low confidence"];
const MISSING_TOKENS = ["缺失", "空白", "未作答", "没作答", "missing", "blank", "empty"];
const RECOGNIZED_TOKENS = ["已识别", "正常", "完整", "recognized", "ready"];
const IDENTITY_TOKENS = ["身份异常", "身份待复核", "学号异常", "姓名异常", "identity"];

export function formatSubmissionFlag(flag: string, locale: Locale): string {
  if (flag.startsWith("recognition_invalid_fields:")) {
    const fields: Record<string, [string, string]> = {
      number: ["题号", "question number"], type: ["题型", "question type"],
      content: ["作答内容", "answer content"], flag: ["识别提示", "recognition notes"],
      recognition_confidence: ["识别置信度", "recognition confidence"],
    };
    const names = [...new Set(flag.slice("recognition_invalid_fields:".length).split(","))]
      .flatMap(field => fields[field] ? [fields[field][locale === "zh-CN" ? 0 : 1]] : []);
    return locale === "zh-CN" ? `待核对：${names.join("、") || "部分识别信息"}`
      : `Review needed: ${names.join(", ") || "some recognition details"}`;
  }
  const labels: Record<string, [string, string]> = {
    recognition_needs_review: ["识别结果需对照原件核对", "Check transcription against the original"],
    external_annotation_present: ["原件含外部批注，不属于学生作答", "External annotations are not student answers"],
    authorship_uncertain: ["部分笔迹归属不明，需核对", "Some writing has uncertain authorship"],
    ocr_only_structure_requires_teacher_review: ["题号匹配需教师核对", "Question matching needs teacher review"],
    recognition_failed: ["识别失败，请修正并确认", "Recognition failed; correct and confirm"],
    recognition_manual_pending: ["等待人工补齐并确认", "Awaiting manual completion and confirmation"],
    recognition_metadata_review: ["识别元信息待核对确认", "Confirm the recognition metadata"],
    recognition_paused: ["已暂停，尚未完成识别", "Paused before recognition completed"],
    recognition_incomplete: ["尚未完成识别", "Recognition incomplete"],
    manual_blank_confirmed: ["教师已确认未作答", "Teacher confirmed no answer"],
    parse_failed: ["作答解析失败，请修正并确认", "Answer parsing failed; correct and confirm"],
    no_matching_answer: ["未识别到对应作答，请对照原件修正并确认", "No matching answer recognized; correct against the source and confirm"],
    recognition_repaired_needs_confirmation: ["识别问题已修正，请确认作答", "Recognition issue corrected; confirm the answer"],
  };
  const label = labels[flag];
  return label ? label[locale === "zh-CN" ? 0 : 1] : flag;
}

export function buildSubmissionQuestions(
  problems: ProblemInfo[],
  students: StudentSubmission[],
): SubmissionQuestion[] {
  const questions: SubmissionQuestion[] = problems.map((problem) => ({
    id: problem.q_id,
    label: problem.number || problem.q_id,
    type: problem.type || "",
    stem: problem.stem || "",
  }));
  const seen = new Set(questions.map((question) => question.id));

  for (const student of students) {
    for (const answer of student.stu_ans ?? []) {
      if (seen.has(answer.q_id)) continue;
      seen.add(answer.q_id);
      questions.push({
        id: answer.q_id,
        label: answer.number || answer.q_id,
        type: answer.type || "",
        stem: "",
      });
    }
  }

  return questions.sort((a, b) => naturalCompare(a.label, b.label));
}

export function getAnswerState(answer?: StudentAnswerInfo): SubmissionAnswerState {
  if (!answer) return "missing";
  if (answerReviewAttention(answer) !== "none") return "flagged";
  if (answer.review_status === "confirmed") return "reviewed";
  if (!answer.content?.trim()) return "empty";
  return "recognized";
}

/** Only concrete missing content blocks; usable uncertainty is advisory. */
export function answerReviewAttention(answer?: StudentAnswerInfo): ReviewAttention {
  if (answerReviewNextBlocked(answer)) return "blocking";
  return answerHasOptionalAdvice(answer) ? "advisory" : "none";
}

/** Suggestions appear in the queue and beside the individual confirmation. */
export function answerHasOptionalAdvice(answer?: StudentAnswerInfo): boolean {
  if (!answer || answer.review_status === "confirmed") return false;
  const confidence = confidenceValue(answer.recognition_confidence);
  return Boolean(answer.flag?.some(flag => ["authorship_uncertain", "external_annotation_present"].includes(flag)))
    || confidenceNeedsAttention(confidence);

}

export function answerNeedsConfirmation(answer?: StudentAnswerInfo): boolean {
  return Boolean(answer && (answer.review_status !== "confirmed" || answerReviewBlocked(answer)));
}

export function getSubmissionReviewStats(
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
): SubmissionReviewStats {
  let answeredCells = 0;
  let reviewCells = 0;
  let identityAnomalies = 0;

  for (const student of students) {
    if (student.identity_status === "needs_review") identityAnomalies += 1;
    const answers = new Map((student.stu_ans ?? []).map((answer) => [answer.q_id, answer]));
    for (const question of questions) {
      const answer = answers.get(question.id);
      if (answer?.content?.trim()) answeredCells += 1;
      if (answerNeedsConfirmation(answer)) reviewCells += 1;
    }
  }

  return {
    students: students.length,
    questions: questions.length,
    expectedCells: students.length * questions.length,
    answeredCells,
    reviewCells,
    identityAnomalies,
    identityMatched: Math.max(students.length - identityAnomalies, 0),
  };
}

export function selectSubmissionReview(
  students: StudentSubmission[],
  allQuestions: SubmissionQuestion[],
  query: string,
  filter: SubmissionReviewFilter,
  sort: SubmissionReviewSort,
): SubmissionReviewSelection {
  const normalized = normalize(query);
  const confidenceAlias = includesToken(normalized, CONFIDENCE_TOKENS);
  const wantsReview = confidenceAlias || includesToken(normalized, REVIEW_TOKENS);
  const wantsMissing = includesToken(normalized, MISSING_TOKENS);
  const wantsRecognized = includesToken(normalized, RECOGNIZED_TOKENS);
  const wantsIdentity = includesToken(normalized, IDENTITY_TOKENS);
  const explicitQuestionTokens = getQuestionTokens(normalized);
  const residual = stripTokens(normalized, [
    ...REVIEW_TOKENS,
    ...CONFIDENCE_TOKENS,
    ...MISSING_TOKENS,
    ...RECOGNIZED_TOKENS,
    ...IDENTITY_TOKENS,
    ...explicitQuestionTokens.raw,
  ]);

  const descriptorMatches = normalized
    ? allQuestions.filter((question) => {
        const descriptor = normalize(`${question.label} ${question.id} ${question.type} ${question.stem}`);
        if (explicitQuestionTokens.values.some((token) => matchesQuestion(question, token))) return true;
        return residual.length >= 2 && descriptor.includes(residual);
      })
    : [];
  const questionScoped = explicitQuestionTokens.values.length > 0 || descriptorMatches.length > 0;
  const questions = questionScoped ? descriptorMatches : allQuestions;
  const scopedQuestions = questions.length > 0 ? questions : allQuestions;

  const selected = students.filter((student) => {
    const identityNeedsReview = student.identity_status === "needs_review";
    if ((filter === "identity" || wantsIdentity) && !identityNeedsReview) return false;

    const answers = new Map((student.stu_ans ?? []).map((answer) => [answer.q_id, answer]));
    const states = scopedQuestions.map((question) => getAnswerState(answers.get(question.id)));
    if ((filter === "review" || wantsReview) && !identityNeedsReview && !scopedQuestions.some(question => answerNeedsConfirmation(answers.get(question.id)))) return false;
    if ((filter === "missing" || wantsMissing) && !states.some((state) => state === "missing" || state === "empty")) return false;
    if (wantsRecognized && !states.some((state) => state === "recognized")) return false;

    if (!residual || questionScoped) return true;
    return normalize(`${student.stu_id} ${student.stu_name}`).includes(residual);
  });

  selected.sort((a, b) => compareStudents(a, b, scopedQuestions, sort));

  let explanation: SubmissionReviewSelection["explanation"] = "all";
  if (selected.length === 0 || (questionScoped && descriptorMatches.length === 0)) explanation = "no_match";
  else if (filter === "identity" || wantsIdentity) explanation = "identity";
  else if (filter === "missing" || wantsMissing) explanation = "missing";
  else if (filter === "review" || wantsReview) explanation = "review";
  else if (questionScoped) explanation = "question";
  else if (residual) explanation = "student";

  return {
    students: selected,
    questions: questionScoped ? descriptorMatches : allQuestions,
    explanation,
    confidenceAlias,
  };
}

export function answerMap(student: StudentSubmission): Map<string, StudentAnswerInfo> {
  return new Map((student.stu_ans ?? []).map((answer) => [answer.q_id, answer]));
}

export function studentNeedsAttention(student: StudentSubmission, questions: SubmissionQuestion[]): boolean {
  if (studentIdentityReviewBlocked(student)) return true;
  const answers = answerMap(student);
  return questions.some((question) => answerReviewAttention(answers.get(question.id)) !== "none");
}

function compareStudents(
  a: StudentSubmission,
  b: StudentSubmission,
  questions: SubmissionQuestion[],
  sort: SubmissionReviewSort,
) {
  if (sort === "attention") {
    const delta = Number(studentNeedsAttention(b, questions)) - Number(studentNeedsAttention(a, questions));
    if (delta) return delta;
  }
  if (sort === "student_name") {
    const byName = naturalCompare(a.stu_name || a.stu_id, b.stu_name || b.stu_id);
    if (byName) return byName;
  }
  return naturalCompare(a.stu_id, b.stu_id);
}

function getQuestionTokens(query: string): { raw: string[]; values: string[] } {
  const raw = query.match(/(?:q\s*\d+(?:[.-]\d+)?|第\s*\d+(?:[.-]\d+)?\s*题)/gi) ?? [];
  return {
    raw,
    values: raw.map((token) => token.replace(/第|题|\s/gi, "").replace(/^q/i, "")),
  };
}

function matchesQuestion(question: SubmissionQuestion, token: string) {
  const candidates = [question.label, question.id]
    .map((value) => normalize(value).replace(/^q/i, ""));
  return candidates.some((value) => value === token || value.endsWith(token));
}

function includesToken(query: string, tokens: string[]) {
  return tokens.some((token) => query.includes(token));
}

function stripTokens(query: string, tokens: string[]) {
  let next = query;
  for (const token of tokens.sort((a, b) => b.length - a.length)) {
    next = next.replaceAll(token, " ");
  }
  return next.replace(/[，,。；;：:、/]+/g, " ").replace(/\s+/g, " ").trim();
}

function normalize(value: string) {
  return value.trim().toLocaleLowerCase();
}

function naturalCompare(a: string, b: string) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

export function answerReviewLabels(answer: StudentAnswerInfo | undefined, locale: Locale): string[] {
  return answerReviewNotes(answer, locale).map(issue => issue.label);
}

export function answerReviewNotes(answer: StudentAnswerInfo | undefined, locale: Locale): Array<{ label: string; severity: "blocking" | "advisory" }> {
  if (!answer || (answer.review_status === "confirmed" && !answerReviewBlocked(answer))) return [];
  const confidence = confidenceValue(answer.recognition_confidence);
  const metadataOnly = answerMetadataConfirmationAllowed(answer);
  const flags = (answer.flag ?? []).filter(flag => !["manual_blank_confirmed", "recognition_needs_review", "recognition_metadata_review", "ocr_only_structure_requires_teacher_review",
    "recognition_very_low_confidence", "very_low_confidence", "recognition_low_confidence", "low_confidence"].includes(flag)
    && !flag.startsWith("recognition_confidence:") && !flag.startsWith("answer_recognition_confidence:")
    && !flag.startsWith("recognition_failed_pages:") && !flag.startsWith("recognition_unprocessed_pages:")
    && !(metadataOnly && ["recognition_failed", "recognition_manual_pending"].includes(flag)));
  const required = new Set(["recognition_failed", "parse_failed", "no_matching_answer", "recognition_manual_pending", "recognition_paused", "recognition_incomplete", "recognition_repaired_needs_confirmation"]);
  return [
    ...(confidenceNeedsAttention(confidence) ? [{ label: locale === "zh-CN" ? `置信度 ${confidence} 较低，建议核对确认。` : `Low confidence (${confidence}); review suggested.`, severity: "advisory" as const }] : []),
    ...[...new Set(flags)].map(flag => ({ label: formatSubmissionFlag(flag, locale), severity: required.has(flag) || flag.startsWith("recognition_invalid_fields:") ? "blocking" as const : "advisory" as const })),
  ];
}
