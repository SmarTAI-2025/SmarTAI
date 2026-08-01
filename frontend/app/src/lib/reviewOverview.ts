import type { Correction } from "@/types";
import type { QuestionSummary, ResultsModel, StudentSummary } from "@/components/tasks/resultsModel";
import { getExpertScoreSpread, type ReviewItem } from "@/components/tasks/resultsReviewModel";

export const reviewCellKey = (studentId: string, questionId: string) => `${studentId}::${questionId}`;

export interface ReviewOverviewSelection {
  students: StudentSummary[];
  questions: QuestionSummary[];
  matchedCellKeys: Set<string>;
  explanation: "all" | "low-confidence" | "disagreement" | "review" | "annotated" | "score" | "text" | "no-match";
}

const LOW_CONFIDENCE_TOKENS = ["低置信", "置信度低", "low confidence"];
const DISAGREEMENT_TOKENS = ["专家分歧", "分歧大", "评分差异", "disagreement", "score spread"];
const REVIEW_TOKENS = ["待复核", "需复核", "复核项", "review", "flagged"];
const ANNOTATED_TOKENS = ["已批注", "教师批注", "有批注", "annotated", "commented"];

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
    };
  }

  const wantsLowConfidence = includesAny(normalized, LOW_CONFIDENCE_TOKENS);
  const wantsDisagreement = includesAny(normalized, DISAGREEMENT_TOKENS);
  const wantsReview = includesAny(normalized, REVIEW_TOKENS);
  const wantsAnnotated = includesAny(normalized, ANNOTATED_TOKENS);
  const scoreLimit = parseScoreLimit(normalized);
  const questionTokens = getQuestionTokens(normalized);
  const reviewKeys = new Set(reviewItems.map((item) => reviewCellKey(item.student.id, item.question.id)));
  const residual = stripQuery(normalized, [
    ...LOW_CONFIDENCE_TOKENS,
    ...DISAGREEMENT_TOKENS,
    ...REVIEW_TOKENS,
    ...ANNOTATED_TOKENS,
    ...questionTokens.raw,
    ...(scoreLimit ? [scoreLimit.raw] : []),
  ]);

  const matchedCellKeys = new Set<string>();
  for (const student of model.students) {
    for (const correction of student.corrections) {
      const question = model.questions.find((item) => item.id === correction.q_id);
      const key = reviewCellKey(student.id, correction.q_id);
      if (wantsLowConfidence && correction.confidence >= 0.65) continue;
      if (wantsDisagreement && !isExpertDisagreement(correction)) continue;
      if (wantsReview && !reviewKeys.has(key)) continue;
      if (wantsAnnotated && !annotatedKeys.has(key)) continue;
      if (scoreLimit && correctionPercent(correction) >= scoreLimit.value) continue;
      if (questionTokens.values.length && !questionTokens.values.some((token) => matchesQuestion(question, correction.q_id, token))) continue;
      if (residual && !cellDescriptor(student, question, correction).includes(residual)) continue;
      matchedCellKeys.add(key);
    }
  }

  const students = model.students.filter((student) =>
    student.corrections.some((correction) => matchedCellKeys.has(reviewCellKey(student.id, correction.q_id))),
  );
  const questions = model.questions.filter((question) =>
    students.some((student) => matchedCellKeys.has(reviewCellKey(student.id, question.id))),
  );

  let explanation: ReviewOverviewSelection["explanation"] = "text";
  if (!matchedCellKeys.size) explanation = "no-match";
  else if (wantsLowConfidence) explanation = "low-confidence";
  else if (wantsDisagreement) explanation = "disagreement";
  else if (wantsReview) explanation = "review";
  else if (wantsAnnotated) explanation = "annotated";
  else if (scoreLimit) explanation = "score";

  return { students, questions, matchedCellKeys, explanation };
}

function correctionPercent(correction: Correction): number {
  return correction.max_score > 0 ? (correction.score / correction.max_score) * 100 : 0;
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
  const match = query.match(/(?:低于|小于|少于|below|under)\s*(\d{1,3})(?:\s*分|\s*%|\s*percent)?/i);
  if (!match) return null;
  return { raw: match[0], value: Math.max(0, Math.min(100, Number(match[1]))) };
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
