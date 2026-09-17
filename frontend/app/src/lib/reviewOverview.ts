import { EMPTY_FILTER_INTENT, matchesQuestionToken, parseLocalTaskFilter, supportsFilterIntent } from "@/lib/taskFilterIntent";
import { compareValues } from "@/lib/sortValues";
import type { Correction, FilterIntentResult } from "@/types";
import {
  correctionScoreSource,
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
const CONFIRMED_REVIEW_TOKENS = ["已复核", "复核完成", "已确认", "教师已处理", "reviewed", "confirmed", "teacher handled"];
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
  const wantsConfirmedReview = includesAny(normalized, CONFIRMED_REVIEW_TOKENS);
  const wantsReview = !wantsNoReview && !wantsConfirmedReview && includesAny(normalized, REVIEW_TOKENS);
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
    ...CONFIRMED_REVIEW_TOKENS,
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
      const confirmedReview = isConfirmedReview(correction);
      if (wantsReview && (!reviewKeys.has(key) || confirmedReview)) continue;
      if (wantsConfirmedReview && (!reviewKeys.has(key) || !confirmedReview)) continue;
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
  if (requestedSort) students.sort((left, right) => compareReviewStudents(left, right, requestedSort.sort, reviewKeys));
  const questions = model.questions.filter((question) =>
    students.some((student) => matchedCellKeys.has(reviewCellKey(student.id, question.id))),
  );

  let explanation: ReviewOverviewSelection["explanation"] = "text";
  if (!matchedCellKeys.size) explanation = "no-match";
  else if (wantsLowConfidence) explanation = "low-confidence";
  else if (wantsDisagreement) explanation = "disagreement";
  else if (wantsReview || wantsConfirmedReview) explanation = "review";
  else if (wantsAnnotated) explanation = "annotated";
  else if (scoreLimit || scoreFloor || wantsUnscored) explanation = "score";

  return { students, questions, matchedCellKeys, explanation, unresolvedText: residual };
}

export function selectReviewOverviewFromIntent(
  model: ResultsModel, reviewItems: ReviewItem[], annotatedKeys: Set<string>, intent: FilterIntentResult,
): ReviewOverviewSelection {
  if (!supportsFilterIntent(intent, "review_overview")) return selectReviewOverview(model, reviewItems, annotatedKeys, "");
  const reviewKeys = new Set(reviewItems.map((item) => reviewCellKey(item.student.id, item.question.id)));
  const matchedCellKeys = new Set<string>();
  for (const student of model.students) {
    if (intent.min_score_percent != null && (student.percent == null || student.percent < intent.min_score_percent)) continue;
    if (intent.max_score_percent != null && (student.percent == null || student.percent >= intent.max_score_percent)) continue;
    if (intent.pass_status === "unscored" && student.percent != null) continue;
    if (intent.pass_status === "pass" && (student.percent == null || student.percent < 60)) continue;
    if (intent.pass_status === "fail" && (student.percent == null || student.percent >= 60)) continue;
    for (const correction of student.corrections) {
      const question = model.questions.find((item) => item.id === correction.q_id);
      const key = reviewCellKey(student.id, correction.q_id);
      const confidence = Number.isFinite(correction.confidence) ? (correction.confidence > 1 ? correction.confidence / 100 : correction.confidence) : null;
      if (intent.low_confidence && (confidence == null || confidence >= 0.65)) continue;
      if (intent.disagreement && !isExpertDisagreement(correction)) continue;
      if (intent.annotated && !annotatedKeys.has(key)) continue;
      if (intent.review_status === "pending" && (!reviewKeys.has(key) || isConfirmedReview(correction))) continue;
      if (intent.review_status === "confirmed" && (!reviewKeys.has(key) || !isConfirmedReview(correction))) continue;
      if (intent.review_status === "none" && reviewKeys.has(key)) continue;
      if (intent.question_tokens.length && !intent.question_tokens.some((token) => matchesQuestionToken(correction.q_id, question?.label ?? correction.q_id, token))) continue;
      if (!intent.text_terms.every((term) => cellDescriptor(student, question, correction).includes(normalize(term)))) continue;
      matchedCellKeys.add(key);
    }
  }
  const students = model.students.filter((student) => student.corrections.some((correction) => matchedCellKeys.has(reviewCellKey(student.id, correction.q_id))));
  if (intent.sort) students.sort((a, b) => compareReviewStudents(a, b, intent.sort!, reviewKeys));
  const questions = model.questions.filter((question) => students.some((student) => matchedCellKeys.has(reviewCellKey(student.id, question.id))));
  return { students, questions, matchedCellKeys, unresolvedText: "", explanation: matchedCellKeys.size ? "text" : "no-match" };
}

export function resolveReviewFilter(model: ResultsModel, reviewItems: ReviewItem[], annotatedKeys: Set<string>, query: string): FilterIntentResult | null {
  const preset = parseLocalTaskFilter(query, "review_overview");
  if (preset) return preset;
  const selection = selectReviewOverview(model, reviewItems, annotatedKeys, query);
  if (reviewQueryNeedsIntentFallback(selection)) return null;
  const normalized = normalize(query);
  const noReview = includesAny(normalized, NO_REVIEW_TOKENS);
  const confirmed = includesAny(normalized, CONFIRMED_REVIEW_TOKENS);
  return { ...EMPTY_FILTER_INTENT,
    min_score_percent: parseScoreFloor(normalized)?.value ?? null,
    max_score_percent: parseScoreLimit(normalized)?.value ?? null,
    pass_status: includesAny(normalized, UNSCORED_TOKENS) ? "unscored" : null,
    low_confidence: includesAny(normalized, LOW_CONFIDENCE_TOKENS),
    disagreement: includesAny(normalized, DISAGREEMENT_TOKENS),
    annotated: includesAny(normalized, ANNOTATED_TOKENS),
    review_status: noReview ? "none" : confirmed ? "confirmed" : includesAny(normalized, REVIEW_TOKENS) ? "pending" : null,
    sort: parseReviewSort(normalized)?.sort ?? null,
    question_tokens: getQuestionTokens(normalized).values,
    text_terms: selection.unresolvedText ? [selection.unresolvedText] : [],
  };
}

export function reviewQueryNeedsIntentFallback(selection: ReviewOverviewSelection): boolean {
  return Boolean(selection.unresolvedText && (selection.matchedCellKeys.size === 0 || /不要|排除|不是|或者|(?:^|\s)(?:not|except|or)(?:\s|$)/i.test(selection.unresolvedText)));
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
    .some((value) => explicitQuestionTokens(value).includes(token));
}

function explicitQuestionTokens(value: string): string[] {
  const normalized = normalize(value);
  const tokens = new Set<string>();
  const direct = normalized.match(/^q?\s*(\d+(?:[.-]\d+)?)$/i);
  if (direct) tokens.add(direct[1]);
  for (const match of normalized.matchAll(/(?:^|[^a-z0-9])(?:q|question|problem)\s*[-_:.]?\s*(\d+(?:[.-]\d+)?)(?=$|[^0-9.])/gi)) {
    tokens.add(match[1]);
  }
  for (const match of normalized.matchAll(/(?:第\s*)?(\d+(?:[.-]\d+)?)\s*题/g)) {
    tokens.add(match[1]);
  }
  return Array.from(tokens);
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
  const preset = parseLocalTaskFilter(query, "review_overview");
  if (preset?.sort) return { raw: query, sort: preset.sort };
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

function compareReviewStudents(left: StudentSummary, right: StudentSummary, sort: NonNullable<FilterIntentResult["sort"]>, reviewKeys: Set<string>): number {
  const direction = sort.endsWith("_desc") ? "desc" : "asc";
  if (sort.startsWith("name_")) return direction === "asc" ? compareNames(left, right) : compareNames(right, left);
  const value = (student: StudentSummary) => sort.startsWith("id_") ? student.id : sort.startsWith("score_") ? student.percent
    : sort.startsWith("confidence_") ? student.avgConfidence
      : student.corrections.filter((correction) => reviewKeys.has(reviewCellKey(student.id, correction.q_id))).length;
  return compareValues(value(left), value(right), direction) || compareNames(left, right);
}

function isConfirmedReview(correction: Correction): boolean {
  const source = correctionScoreSource(correction);
  return source === "teacher_confirmed_same" || source === "teacher_changed";
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
