import type { FilterIntentResult, ProblemInfo, StudentAnswerInfo, StudentSubmission } from "@/types";

export type SubmissionAnswerState = "recognized" | "reviewed" | "flagged" | "empty" | "missing";
export type SubmissionReviewFilter = "all" | "review" | "missing" | "identity";
export type SubmissionQuestionSort = `question:${string}:asc` | `question:${string}:desc`;
export type SubmissionReviewSort =
  | "id_asc"
  | "id_desc"
  | "name_asc"
  | "name_desc"
  | "coverage_asc"
  | "coverage_desc"
  | "review_asc"
  | "review_desc"
  | SubmissionQuestionSort;

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
  const requestedSort = parseSubmissionReviewLocalSort(normalized);
  const residual = stripQueryWords(stripTokens(normalized, [
    ...REVIEW_TOKENS,
    ...CONFIDENCE_TOKENS,
    ...MISSING_TOKENS,
    ...RECOGNIZED_TOKENS,
    ...IDENTITY_TOKENS,
    ...explicitQuestionTokens.raw,
    ...(requestedSort ? [requestedSort.raw] : []),
  ]));

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

export function selectSubmissionReviewFromIntent(
  students: StudentSubmission[],
  allQuestions: SubmissionQuestion[],
  intent: FilterIntentResult,
  filter: SubmissionReviewFilter,
  sort: SubmissionReviewSort,
): SubmissionReviewSelection {
  const requestedQuestions = intent.question_tokens.length
    ? allQuestions.filter((question) => intent.question_tokens.some((token) => matchesQuestion(question, normalizeQuestionToken(token))))
    : allQuestions;
  const questions = intent.question_tokens.length ? requestedQuestions : allQuestions;
  const scopedQuestions = questions.length ? questions : allQuestions;
  const selected = students.filter((student) => {
    if (!matchesFilter(student, scopedQuestions, filter)) return false;
    if (!matchesSubmissionStatus(student, scopedQuestions, intent.submission_status ?? null)) return false;
    return intent.text_terms.every((term) => matchesIntentTextTerm(student, scopedQuestions, term));
  });
  selected.sort((left, right) => compareStudents(left, right, scopedQuestions, sort));

  let explanation: SubmissionReviewSelection["explanation"] = "all";
  if (!selected.length || (intent.question_tokens.length > 0 && !requestedQuestions.length)) explanation = "no_match";
  else if (filter === "identity" || intent.submission_status === "identity") explanation = "identity";
  else if (filter === "missing" || intent.submission_status === "missing") explanation = "missing";
  else if (filter === "review" || intent.submission_status === "review") explanation = "review";
  else if (intent.question_tokens.length) explanation = "question";
  else if (intent.text_terms.length) explanation = "student";

  return { students: selected, questions, explanation, confidenceAlias: false };
}

export function submissionReviewQueryNeedsIntentFallback(
  query: string,
  students: StudentSubmission[],
  questions: SubmissionQuestion[],
): boolean {
  const normalized = normalize(query);
  if (!normalized) return false;
  const questionTokens = getQuestionTokens(normalized);
  const requestedSort = parseSubmissionReviewLocalSort(normalized);
  let residual = stripQueryWords(stripTokens(normalized, [
    ...REVIEW_TOKENS,
    ...CONFIDENCE_TOKENS,
    ...MISSING_TOKENS,
    ...RECOGNIZED_TOKENS,
    ...IDENTITY_TOKENS,
    ...questionTokens.raw,
    ...(requestedSort ? [requestedSort.raw] : []),
  ]));
  if (!residual) return false;

  const identityMatches = students.some((student) => normalize(`${student.stu_id} ${student.stu_name}`).includes(residual));
  if (identityMatches) return false;
  const questionMatches = questions.some((question) => normalize(`${question.label} ${question.id} ${question.type} ${question.stem}`).includes(residual));
  return !questionMatches;
}

export function parseSubmissionReviewLocalSort(query: string): { raw: string; sort: SubmissionReviewSort } | null {
  const patterns: Array<[RegExp, SubmissionReviewSort]> = [
    [/(?:按)?(?:学号|学生\s*id|id)\s*(?:降序|从[大高]到[小低]|z[\s-]*a)|(?:sort\s+(?:by\s+)?)?(?:student\s*)?id\s*(?:desc(?:ending)?|z[\s-]*a)/i, "id_desc"],
    [/(?:按)?(?:学号|学生\s*id|id)\s*(?:升序|从[小低]到[大高]|a[\s-]*z)|(?:sort\s+(?:by\s+)?)?(?:student\s*)?id\s*(?:asc(?:ending)?|a[\s-]*z)/i, "id_asc"],
    [/(?:按)?(?:姓名|名字|name)\s*(?:降序|从[大高]到[小低]|z[\s-]*a)|(?:sort\s+(?:by\s+)?)?name\s*(?:desc(?:ending)?|z[\s-]*a)/i, "name_desc"],
    [/(?:按)?(?:姓名|名字|name)\s*(?:升序|从[小低]到[大高]|a[\s-]*z)|(?:sort\s+(?:by\s+)?)?name\s*(?:asc(?:ending)?|a[\s-]*z)/i, "name_asc"],
    [/(?:作答)?覆盖率\s*(?:从低到高|升序)|coverage\s*(?:asc|low)/i, "coverage_asc"],
    [/(?:作答)?覆盖率\s*(?:从高到低|降序)|coverage\s*(?:desc|high)/i, "coverage_desc"],
    [/(?:待复核|异常|需复核|复核项|review)\s*(?:从少到多|升序)|review\s*(?:asc|few)/i, "review_asc"],
    [/(?:待复核|异常|需复核|复核项|review)\s*(?:最多|优先|从多到少|降序)|review\s*(?:desc|most)/i, "review_desc"],
  ];
  for (const [pattern, sort] of patterns) {
    const match = query.match(pattern);
    if (match) return { raw: match[0], sort };
  }
  return null;
}

export function submissionReviewSortFromIntent(intent: FilterIntentResult): SubmissionReviewSort | null {
  switch (intent.sort) {
    case "id_asc": return "id_asc";
    case "id_desc": return "id_desc";
    case "name_asc": return "name_asc";
    case "name_desc": return "name_desc";
    case "coverage_asc": return "coverage_asc";
    case "coverage_desc": return "coverage_desc";
    case "review_asc": return "review_asc";
    case "review_desc": return "review_desc";
    default: return null;
  }
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
  const questionSort = parseSubmissionQuestionSort(sort);
  if (questionSort) {
    const leftSeverity = answerStateSeverity(getAnswerState(answerMap(a).get(questionSort.questionId)));
    const rightSeverity = answerStateSeverity(getAnswerState(answerMap(b).get(questionSort.questionId)));
    const delta = leftSeverity - rightSeverity;
    if (delta) return questionSort.direction === "asc" ? delta : -delta;
  }
  if (sort === "id_desc") return naturalCompare(b.stu_id, a.stu_id);
  if (sort === "name_asc") return compareNames(a, b);
  if (sort === "name_desc") return compareNames(b, a);
  if (sort === "coverage_asc" || sort === "coverage_desc") {
    const delta = coverageCount(a, questions) - coverageCount(b, questions);
    if (delta) return sort === "coverage_asc" ? delta : -delta;
  }
  if (sort === "review_asc" || sort === "review_desc") {
    const delta = reviewCount(a, questions) - reviewCount(b, questions);
    if (delta) return sort === "review_asc" ? delta : -delta;
  }
  return naturalCompare(a.stu_id, b.stu_id);
}

export function parseSubmissionQuestionSort(value: string | null | undefined): { questionId: string; direction: "asc" | "desc" } | null {
  const match = value?.match(/^question:(.+):(asc|desc)$/);
  return match ? { questionId: match[1], direction: match[2] as "asc" | "desc" } : null;
}

/** Lower values are safer answer states; equal states retain the stable student-ID order. */
function answerStateSeverity(state: SubmissionAnswerState): number {
  switch (state) {
    case "recognized":
    case "reviewed":
      return 0;
    case "flagged":
      return 1;
    case "empty":
      return 2;
    case "missing":
      return 3;
  }
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

function matchesFilter(student: StudentSubmission, questions: SubmissionQuestion[], filter: SubmissionReviewFilter): boolean {
  if (filter === "all") return true;
  return matchesSubmissionStatus(student, questions, filter === "identity" ? "identity" : filter);
}

function matchesSubmissionStatus(
  student: StudentSubmission,
  questions: SubmissionQuestion[],
  status: NonNullable<FilterIntentResult["submission_status"]> | null,
): boolean {
  if (!status) return true;
  if (status === "identity") return student.identity_status === "needs_review";
  const states = questions.map((question) => getAnswerState(answerMap(student).get(question.id)));
  if (status === "review") return states.some((state) => !["recognized", "reviewed"].includes(state));
  if (status === "missing") return states.some((state) => state === "missing" || state === "empty");
  if (status === "reviewed") return states.length > 0 && states.every((state) => state === "reviewed");
  return states.length > 0 && states.every((state) => ["recognized", "reviewed"].includes(state)) && states.some((state) => state === "recognized");
}

function matchesIntentTextTerm(student: StudentSubmission, questions: SubmissionQuestion[], term: string): boolean {
  const normalizedTerm = normalize(term);
  if (!normalizedTerm) return true;
  const studentDescriptor = normalize(`${student.stu_id} ${student.stu_name}`);
  if (studentDescriptor.includes(normalizedTerm)) return true;
  return questions.some((question) => normalize(`${question.label} ${question.id} ${question.type} ${question.stem}`).includes(normalizedTerm));
}

function coverageCount(student: StudentSubmission, questions: SubmissionQuestion[]): number {
  const answers = answerMap(student);
  return questions.filter((question) => {
    const state = getAnswerState(answers.get(question.id));
    return state === "recognized" || state === "reviewed";
  }).length;
}

function reviewCount(student: StudentSubmission, questions: SubmissionQuestion[]): number {
  const answers = answerMap(student);
  return questions.filter((question) => !["recognized", "reviewed"].includes(getAnswerState(answers.get(question.id)))).length
    + Number(student.identity_status === "needs_review");
}

function compareNames(left: StudentSubmission, right: StudentSubmission): number {
  return naturalCompare(left.stu_name || left.stu_id, right.stu_name || right.stu_id)
    || naturalCompare(left.stu_id, right.stu_id);
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

function stripQueryWords(query: string): string {
  return query
    .replace(/(?:帮我|请|显示|查看|筛选|找出|学生|同学|作答|答案|哪些|所有|的|了|一下|按|排序|排列|show|filter|find|students?|responses?|answers?|please|the|with|and)/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeQuestionToken(value: string): string {
  return normalize(value).replace(/第|题|\s/gi, "").replace(/^q/i, "");
}

function normalize(value: string) {
  return value.trim().toLocaleLowerCase();
}

function naturalCompare(a: string, b: string) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}
