import type { ProblemInfo, StudentAnswerInfo, StudentSubmission } from "@/types";

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

const REVIEW_TOKENS = ["待复核", "需复核", "异常", "有问题", "review", "flagged", "flag"];
const CONFIDENCE_TOKENS = ["低置信", "置信度低", "low confidence"];
const MISSING_TOKENS = ["缺失", "空白", "未作答", "没作答", "missing", "blank", "empty"];
const RECOGNIZED_TOKENS = ["已识别", "正常", "完整", "recognized", "ready"];
const IDENTITY_TOKENS = ["身份异常", "身份待复核", "学号异常", "姓名异常", "identity"];

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
  if (answer.review_status === "confirmed") return "reviewed";
  if (!answer.content?.trim()) return "empty";
  if (answer.flag?.length) return "flagged";
  return "recognized";
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
      const state = getAnswerState(answer);
      if (answer?.content?.trim()) answeredCells += 1;
      if (!["recognized", "reviewed"].includes(state)) reviewCells += 1;
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
    if ((filter === "review" || wantsReview) && !states.some((state) => !["recognized", "reviewed"].includes(state))) return false;
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
  if (student.identity_status === "needs_review") return true;
  const answers = answerMap(student);
  return questions.some((question) => !["recognized", "reviewed"].includes(getAnswerState(answers.get(question.id))));
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
