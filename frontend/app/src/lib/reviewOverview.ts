import type { Correction, FilterIntentResult } from "@/types";
import {
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "@/components/tasks/resultsModel";
import { getExpertScoreSpread, type ReviewItem } from "@/components/tasks/resultsReviewModel";

export const reviewCellKey = (studentId: string, questionId: string) => `${studentId}::${questionId}`;

export interface ReviewOverviewSelection {
  students: StudentSummary[];
  questions: QuestionSummary[];
  matchedCellKeys: Set<string>;
  explanation: "all" | "low-confidence" | "disagreement" | "review" | "annotated" | "score" | "text" | "no-match";
  unresolvedText: string;
}

const LOW_CONFIDENCE_TOKENS = ["低置信", "置信度低", "low confidence"];
const DISAGREEMENT_TOKENS = ["专家分歧", "分歧大", "评分差异", "disagreement", "score spread"];
const REVIEW_TOKENS = ["待复核", "需复核", "复核项", "review", "flagged"];
const ANNOTATED_TOKENS = ["已批注", "教师批注", "有批注", "annotated", "commented"];
const NO_REVIEW_TOKENS = ["无复核信号", "无需复核", "no review"];
const UNSCORED_TOKENS = ["无可比总分", "无分", "unscored"];

export function isExpertDisagreement(correction: Correction): boolean {
  return Boolean(
    correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")
      || getExpertScoreSpread(correction) > Math.max(1, correction.max_score * 0.25),
  );
}

export function selectReviewOverview(
  model: ResultsModel,
  reviewItems: ReviewItem[],
  annotatedKeys: Set<string>,
  query: string,
): ReviewOverviewSelection {
  const normalized = normalize(query);
  const allCellKeys = new Set(
    model.students.flatMap((student) => student.corrections.map((correction) => reviewCellKey(student.id, correction.q_id))),
  );
  if (!normalized) {
    return {
      students: model.students,
      questions: model.questions,
      matchedCellKeys: allCellKeys,
      explanation: "all",
      unresolvedText: "",
    };
  }

  const wantsLowConfidence = includesAny(normalized, LOW_CONFIDENCE_TOKENS);
  const wantsDisagreement = includesAny(normalized, DISAGREEMENT_TOKENS);
  const wantsNoReview = includesAny(normalized, NO_REVIEW_TOKENS);
  const wantsReview = !wantsNoReview && includesAny(normalized, REVIEW_TOKENS);
  const wantsAnnotated = includesAny(normalized, ANNOTATED_TOKENS);
  const wantsUnscored = includesAny(normalized, UNSCORED_TOKENS);
  const scoreLimit = parseScoreLimit(normalized);
  const scoreFloor = parseScoreFloor(normalized);
  const questionTokens = getQuestionTokens(normalized);
  const requestedSort = parseReviewSort(normalized);
  const reviewKeys = new Set(reviewItems.map((item) => reviewCellKey(item.student.id, item.question.id)));
  const residual = stripQuery(normalized, [
    ...LOW_CONFIDENCE_TOKENS,
    ...DISAGREEMENT_TOKENS,
    ...REVIEW_TOKENS,
    ...ANNOTATED_TOKENS,
    ...NO_REVIEW_TOKENS,
    ...UNSCORED_TOKENS,
    ...questionTokens.raw,
    ...(scoreLimit ? [scoreLimit.raw] : []),
    ...(scoreFloor ? [scoreFloor.raw] : []),
    ...(requestedSort ? [requestedSort.raw] : []),
  ]);

  const matchedCellKeys = new Set<string>();
  for (const student of model.students) {
    for (const correction of student.corrections) {
      const question = model.questions.find((item) => item.id === correction.q_id);
      const key = reviewCellKey(student.id, correction.q_id);
      if (wantsLowConfidence && correction.confidence >= 0.65) continue;
      if (wantsDisagreement && !isExpertDisagreement(correction)) continue;
      if (wantsReview && !reviewKeys.has(key)) continue;
      if (wantsNoReview && reviewKeys.has(key)) continue;
      if (wantsAnnotated && !annotatedKeys.has(key)) continue;
      if (wantsUnscored && student.percent !== null) continue;
      if (scoreLimit && (student.percent === null || student.percent >= scoreLimit.value)) continue;
      if (scoreFloor && (student.percent === null || student.percent < scoreFloor.value)) continue;
      if (questionTokens.values.length && !questionTokens.values.some((token) => matchesQuestion(question, correction.q_id, token))) continue;
      if (residual && !cellDescriptor(student, question, correction).includes(residual)) continue;
      matchedCellKeys.add(key);
    }
  }

  const students = model.students.filter((student) =>
    student.corrections.some((correction) => matchedCellKeys.has(reviewCellKey(student.id, correction.q_id))),
  );
  if (requestedSort) students.sort((left, right) => compareReviewStudents(left, right, requestedSort.sort));
  const questions = model.questions.filter((question) =>
    students.some((student) => matchedCellKeys.has(reviewCellKey(student.id, question.id))),
  );

  let explanation: ReviewOverviewSelection["explanation"] = "text";
  if (!matchedCellKeys.size) explanation = "no-match";
  else if (wantsLowConfidence) explanation = "low-confidence";
  else if (wantsDisagreement) explanation = "disagreement";
  else if (wantsReview) explanation = "review";
  else if (wantsAnnotated) explanation = "annotated";
  else if (scoreLimit || scoreFloor || wantsUnscored) explanation = "score";

  return { students, questions, matchedCellKeys, explanation, unresolvedText: residual };
}

export function selectReviewOverviewFromIntent(
  model: ResultsModel,
  reviewItems: ReviewItem[],
  annotatedKeys: Set<string>,
  intent: FilterIntentResult,
): ReviewOverviewSelection {
  return selectReviewOverview(
    model,
    reviewItems,
    annotatedKeys,
    reviewIntentCanonicalQuery(intent),
  );
}

export function reviewQueryNeedsIntentFallback(selection: ReviewOverviewSelection): boolean {
  return Boolean(selection.unresolvedText && selection.matchedCellKeys.size === 0);
}

function cellDescriptor(student: StudentSummary, question: QuestionSummary | undefined, correction: Correction): string {
  return normalize([
    student.id,
    student.name,
    question?.id,
    question?.label,
    question?.type,
    question?.stem,
    correction.type,
    correction.comment,
    ...(correction.review_reasons ?? []),
  ].filter(Boolean).join(" "));
}

function getQuestionTokens(query: string): { raw: string[]; values: string[] } {
  const raw = query.match(/(?:q\s*\d+(?:[.-]\d+)?|第\s*\d+(?:[.-]\d+)?\s*题)/gi) ?? [];
  return {
    raw,
    values: raw.map((token) => token.replace(/第|题|\s/gi, "").replace(/^q/i, "")),
  };
}

function matchesQuestion(question: QuestionSummary | undefined, fallbackId: string, token: string): boolean {
  return [question?.label, question?.id, fallbackId]
    .filter((value): value is string => Boolean(value))
    .map((value) => normalize(value).replace(/^q/i, ""))
    .some((value) => value === token || value.endsWith(token));
}

function parseScoreLimit(query: string): { raw: string; value: number } | null {
  const match = query.match(
    /(?:(?:低于|小于|少于|below|under)\s*(\d{1,3})(?:\s*分|\s*%|\s*percent)?|(\d{1,3})\s*(?:分|%)?\s*(?:以下|以内|之下|及以下))/i,
  );
  if (!match) return null;
  return { raw: match[0], value: Math.max(0, Math.min(100, Number(match[1] ?? match[2]))) };
}

function parseScoreFloor(query: string): { raw: string; value: number } | null {
  const match = query.match(/(?:至少|不低于|大于等于|≥|>=|at least)\s*(\d{1,3})(?:\s*分|\s*%| percent)?/i);
  if (!match) return null;
  return { raw: match[0], value: Math.max(0, Math.min(100, Number(match[1]))) };
}

function parseReviewSort(query: string): { raw: string; sort: NonNullable<FilterIntentResult["sort"]> } | null {
  const patterns: Array<[RegExp, NonNullable<FilterIntentResult["sort"]>]> = [
    [/(?:置信度).*(?:从低到高|低到高)|confidence\s*(?:asc|low)/i, "confidence_asc"],
    [/(?:复核信号|复核项).*(?:最多|优先)|review\s*(?:desc|most)/i, "review_desc"],
    [/(?:得分率)?\s*(?:从高到低|高到低|降序)|score\s*(?:desc|high)/i, "score_desc"],
    [/(?:得分率)?\s*(?:从低到高|低到高|升序)|score\s*(?:asc|low)/i, "score_asc"],
  ];
  for (const [pattern, sort] of patterns) {
    const match = query.match(pattern);
    if (match) return { raw: match[0], sort };
  }
  return null;
}

function compareReviewStudents(left: StudentSummary, right: StudentSummary, sort: NonNullable<FilterIntentResult["sort"]>): number {
  if (sort === "score_asc") return nullable(left.percent, Number.POSITIVE_INFINITY) - nullable(right.percent, Number.POSITIVE_INFINITY) || compareNames(left, right);
  if (sort === "score_desc") return nullable(right.percent, Number.NEGATIVE_INFINITY) - nullable(left.percent, Number.NEGATIVE_INFINITY) || compareNames(left, right);
  if (sort === "confidence_asc") return nullable(left.avgConfidence, Number.POSITIVE_INFINITY) - nullable(right.avgConfidence, Number.POSITIVE_INFINITY) || compareNames(left, right);
  const reviewCount = (student: StudentSummary) => student.corrections.filter((correction) => correction.requires_human_review).length;
  return reviewCount(right) - reviewCount(left) || compareNames(left, right);
}

function reviewIntentCanonicalQuery(intent: FilterIntentResult): string {
  const parts: string[] = [];
  if (intent.max_score_percent !== null) parts.push(`低于 ${intent.max_score_percent} 分`);
  if (intent.min_score_percent !== null) parts.push(`至少 ${intent.min_score_percent} 分`);
  if (intent.pass_status === "fail") parts.push("低于 60 分");
  if (intent.pass_status === "pass") parts.push("至少 60 分");
  if (intent.low_confidence) parts.push("低置信");
  if (intent.disagreement) parts.push("专家分歧");
  if (intent.review_status === "pending") parts.push("待复核");
  if (intent.review_status === "confirmed" || intent.annotated) parts.push("已批注");
  if (intent.review_status === "none") parts.push("无复核信号");
  if (intent.pass_status === "unscored") parts.push("无可比总分");
  if (intent.sort === "score_asc") parts.push("得分率从低到高");
  if (intent.sort === "score_desc") parts.push("得分率从高到低");
  if (intent.sort === "confidence_asc") parts.push("置信度从低到高");
  if (intent.sort === "review_desc") parts.push("复核信号最多优先");
  parts.push(...intent.question_tokens, ...intent.text_terms);
  return parts.join(" ");
}

function nullable(value: number | null | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function compareNames(left: StudentSummary, right: StudentSummary): number {
  return left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" }) || left.id.localeCompare(right.id);
}

function stripQuery(query: string, tokens: string[]): string {
  let next = query;
  for (const token of [...tokens].sort((a, b) => b.length - a.length)) {
    next = next.replaceAll(token, " ");
  }
  return next
    .replace(/学生|题次|题目|哪些|所有|查看|显示|筛选|找出|请|的|了|一下/gi, " ")
    .replace(/[，,。；;：:、/]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function includesAny(query: string, tokens: string[]): boolean {
  return tokens.some((token) => query.includes(token));
}

function normalize(value: string): string {
  return value.trim().toLocaleLowerCase();
}
